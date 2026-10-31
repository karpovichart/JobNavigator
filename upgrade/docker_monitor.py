#!/usr/bin/env python3
"""Local Docker monitor. Python 3.9+, Docker CLI. No pip dependencies.
python3 docker_monitor.py [container ...] --duration 1800 --interval 1
Only local unix:// Docker contexts are supported. No container modifications.
"""
import argparse, concurrent.futures, csv, datetime as dt, http.client, json
import os, pathlib, socket, subprocess, sys, time, urllib.parse

class UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__('localhost', timeout=5)
        self.path = path
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)

def api(path, route):
    connection = UnixHTTP(path)
    try:
        connection.request('GET', route)
        response = connection.getresponse()
        data = response.read()
        if response.status != 200:
            raise RuntimeError(f'HTTP {response.status}: {data.decode(errors="replace")[:160]}')
        return json.loads(data)
    finally:
        connection.close()

def metrics(current, previous):
    cpu = None
    if previous:
        now, old = current['cpu_stats'], previous['cpu_stats']
        delta = now['cpu_usage']['total_usage'] - old['cpu_usage']['total_usage']
        system = now.get('system_cpu_usage', 0) - old.get('system_cpu_usage', 0)
        cores = now.get('online_cpus') or len(now['cpu_usage'].get('percpu_usage', []))
        if system > 0 and delta >= 0 and cores:
            cpu = delta / system * cores * 100
    memory = current.get('memory_stats', {})
    # Match Docker CLI's Linux memory accounting (subtract inactive file cache).
    stats = memory.get('stats', {})
    cache = stats.get('total_inactive_file', stats.get('inactive_file', 0))
    usage = memory.get('usage')
    used = (usage - cache if cache < usage else usage) if usage is not None else None
    return cpu, used

PAGE = r'''<!doctype html><meta charset="utf-8"><title>Docker resource report</title>
<style>body{font:15px system-ui;margin:28px;background:#101522;color:#e5eaf3}h1{font-size:24px}section{background:#192133;padding:20px;border-radius:12px;margin:18px 0}canvas{width:100%;height:300px}label{display:inline-block;margin:8px}input{accent-color:#88b8ff}table{border-collapse:collapse;width:100%}td,th{text-align:left;padding:9px;border-bottom:1px solid #35405a}.muted{color:#a6b4ca}#tip{min-height:24px}input[type=range]{width:30%}</style>
<h1>Docker · CPU & memory</h1><p id="meta" class="muted"></p><div id="legend"></div>
<section>Time range: <input id="start" type="range" min="0" max="1000" value="0"><input id="end" type="range" min="0" max="1000" value="1000"><span id="range"></span><p id="tip" class="muted">Hover over a chart to inspect a sample. Toggle containers above.</p></section>
<section><h2>CPU (%) · 100% = one core</h2><canvas id="cpu"></canvas></section><section><h2>Memory (MiB) · inactive file cache excluded</h2><canvas id="ram"></canvas></section><section><h2>Summary · entire recording</h2><table id="summary"></table></section>
<script>
const D=__DATA__, colors=['#6cb6ff','#ffab70','#7ee787','#d2a8ff','#ff7b91','#f2cc60','#56d4dd'];
const names=[...new Set(D.rows.map(r=>r.name))], enabled=new Set(names), duration=Math.max(1,...D.rows.map(r=>r.elapsed_s));
const groups=names.map(n=>D.rows.filter(r=>r.name===n));
const el=id=>document.getElementById(id);el('meta').textContent=`${D.started} · ${D.rows.length} successful samples · target interval ${D.interval}s · ${D.errors} failed requests. Missing samples are gaps; first CPU sample is unavailable.`;
names.forEach((n,i)=>{let label=document.createElement('label'), check=document.createElement('input');check.type='checkbox';check.checked=true;check.onchange=()=>{check.checked?enabled.add(n):enabled.delete(n);draw()};label.style.color=colors[i%colors.length];label.append(check,document.createTextNode(n));el('legend').append(label)});
let head=el('summary').insertRow();['Container','Mean CPU %','Peak CPU %','Mean MiB','Peak MiB','Samples'].forEach(t=>{let c=document.createElement('th');c.textContent=t;head.append(c)});
const avg=a=>a.length?a.reduce((s,v)=>s+v,0)/a.length:null, max=a=>a.length?Math.max(...a):null, fmt=v=>v===null?'—':v.toFixed(2);
groups.forEach((g,i)=>{let cpu=g.map(r=>r.cpu_percent).filter(v=>v!==null),ram=g.map(r=>r.memory_bytes===null?null:r.memory_bytes/1048576).filter(v=>v!==null);let tr=el('summary').insertRow();[names[i],fmt(avg(cpu)),fmt(max(cpu)),fmt(avg(ram)),fmt(max(ram)),g.length].forEach(t=>tr.insertCell().textContent=t)});
function bounds(){let a=+el('start').value/1000*duration,b=+el('end').value/1000*duration;return [Math.min(a,b),Math.max(a,b)+.001]}
function plot(id,key,scale){let canvas=el(id),rect=canvas.getBoundingClientRect(),dpr=devicePixelRatio||1;canvas.width=rect.width*dpr;canvas.height=300*dpr;let c=canvas.getContext('2d');c.scale(dpr,dpr);let w=rect.width,h=300,L=65,R=w-18,T=15,B=265,[a,b]=bounds();let data=groups.map((g,i)=>enabled.has(names[i])?g.filter(r=>r.elapsed_s>=a&&r.elapsed_s<=b):[]);let peak=Math.max(1,...data.flat().map(r=>(r[key]||0)/scale))*1.1;let x=t=>L+(t-a)/(b-a)*(R-L),y=v=>B-v/peak*(B-T);c.font='12px system-ui';for(let j=0;j<=4;j++){let v=peak*j/4,yy=y(v);c.strokeStyle='#35405a';c.beginPath();c.moveTo(L,yy);c.lineTo(R,yy);c.stroke();c.fillStyle='#a6b4ca';c.fillText(v.toFixed(1),2,yy+4);let t=a+(b-a)*j/4;c.fillText((t/60).toFixed(1)+' min',x(t)-18,B+24)}data.forEach((g,i)=>{c.strokeStyle=colors[i%colors.length];c.lineWidth=1.6;c.beginPath();let last=null;g.forEach(r=>{let v=r[key];if(v===null){last=null;return}if(last===null||r.elapsed_s-last>D.interval*2.5)c.moveTo(x(r.elapsed_s),y(v/scale));else c.lineTo(x(r.elapsed_s),y(v/scale));last=r.elapsed_s});c.stroke()});canvas.onmousemove=e=>{let target=a+Math.max(0,Math.min(1,(e.clientX-rect.left-L)/(R-L)))*(b-a),texts=[];data.forEach((g,i)=>{if(!g.length)return;let row=g.reduce((best,r)=>Math.abs(r.elapsed_s-target)<Math.abs(best.elapsed_s-target)?r:best,g[0]);if(Math.abs(row.elapsed_s-target)<=D.interval*2)texts.push(names[i]+': '+(row[key]===null?'—':(row[key]/scale).toFixed(2)))});el('tip').textContent=(target/60).toFixed(2)+' min · '+texts.join(' | ')}}
function draw(){let [a,b]=bounds();el('range').textContent=`${(a/60).toFixed(1)}–${(b/60).toFixed(1)} min`;plot('cpu','cpu_percent',1);plot('ram','memory_bytes',1048576)}el('start').oninput=draw;el('end').oninput=draw;window.onresize=draw;draw();
</script>'''

def report(folder, rows, started, interval, errors):
    payload = json.dumps(dict(rows=rows, started=started, interval=interval, errors=errors), ensure_ascii=True).replace('<', '\\u003c')
    (folder / 'report.html').write_text(PAGE.replace('__DATA__', payload), encoding='utf-8')

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('containers', nargs='*', help='Names/IDs; default: all currently running containers')
    parser.add_argument('--duration', type=float, default=1800, help='Seconds, default 1800')
    parser.add_argument('--interval', type=float, default=1, help='Seconds, default 1')
    parser.add_argument('--output', help='New output directory')
    args = parser.parse_args()
    if args.duration <= 0 or args.interval <= 0:
        parser.error('duration and interval must be positive')
    # Match Docker CLI context precedence; named contexts override DOCKER_HOST.
    context = os.environ.get('DOCKER_CONTEXT')
    host = None if context else os.environ.get('DOCKER_HOST')
    if not host:
        command = ['docker', 'context', 'inspect'] + ([context] if context else [])
        host = json.loads(subprocess.check_output(command, text=True))[0]['Endpoints']['docker']['Host']
    if not host.startswith('unix://'):
        parser.error('Only local unix:// Docker endpoints are supported; select a local Docker context.')
    path = urllib.parse.unquote(host[len('unix://'):])
    containers = api(path, '/containers/json')
    selected = []
    if args.containers:
        for value in args.containers:
            matches = [c for c in containers if c['Id'].startswith(value) or value in [n.lstrip('/') for n in c['Names']]]
            if len(matches) != 1:
                parser.error(f'{value}: expected one running container, found {len(matches)}')
            if matches[0] not in selected:
                selected.append(matches[0])
    else:
        selected = containers
    if not selected:
        parser.error('No running containers found')
    folder = pathlib.Path(args.output or ('docker-metrics-' + dt.datetime.now().strftime('%Y%m%d-%H%M%S')))
    folder.mkdir(parents=True, exist_ok=False)
    started = dt.datetime.now().astimezone().isoformat(timespec='seconds')
    previous, rows, errors = {}, [], 0
    print(f'Monitoring {len(selected)} containers for {args.duration:g}s; target interval {args.interval:g}s')
    print(f'Output: {folder.resolve()} | Ctrl+C saves a partial report', flush=True)
    fields = ['timestamp','elapsed_s','name','container_id','cpu_percent','memory_bytes','engine_read']
    origin = time.monotonic()
    def sample(container):
        data = api(path, '/containers/' + container['Id'] + '/stats?stream=false&one-shot=true')
        return container, data, time.monotonic(), dt.datetime.now().astimezone().isoformat(timespec='milliseconds')
    with (folder / 'metrics.csv').open('w', newline='') as csvfile, (folder / 'errors.log').open('w') as errorfile:
        writer = csv.DictWriter(csvfile, fieldnames=fields); writer.writeheader()
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=min(32,len(selected)))
        tick = 0
        try:
            while time.monotonic() - origin < args.duration:
                futures = [pool.submit(sample, c) for c in selected]
                for future in concurrent.futures.as_completed(futures):
                    try:
                        container, data, stamp, timestamp = future.result()
                        cid = container['Id']
                        cpu, memory = metrics(data, previous.get(cid)); previous[cid] = data
                        row = dict(timestamp=timestamp, elapsed_s=round(stamp-origin, 4), name=container['Names'][0].lstrip('/'), container_id=cid, cpu_percent=cpu, memory_bytes=memory, engine_read=data.get('read'))
                        rows.append(row); writer.writerow(row)
                    except Exception as exc:
                        errors += 1; errorfile.write(f'{dt.datetime.now().isoformat()} {exc}\n'); errorfile.flush()
                csvfile.flush()
                tick = max(tick+1, int((time.monotonic()-origin)/args.interval)+1)
                remaining = min(origin+tick*args.interval, origin+args.duration)-time.monotonic()
                if remaining > 0:
                    time.sleep(remaining)
                if tick % max(1,round(30/args.interval)) == 0:
                    print(f'{min(time.monotonic()-origin,args.duration):.0f}s | {len(rows)} samples | {errors} errors',flush=True)
        except KeyboardInterrupt:
            print('\nStopped; saving partial report.')
        finally:
            pool.shutdown(wait=True)
            rows.sort(key=lambda r:r['elapsed_s'])
            report(folder, rows, started, args.interval, errors)
    print(f'Ready: {folder.resolve() / "report.html"}')
    if not rows:
        print('No samples collected; see errors.log',file=sys.stderr)
        return 1
    return 0

if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, subprocess.SubprocessError, RuntimeError, ValueError) as exc:
        sys.exit(f'Error: {exc}')
