import os, subprocess, sys, time, json, psutil
ROOT = os.environ.get('CV_ROOT', '/home/user/credvault')
label, path = sys.argv[1], sys.argv[2]
env = dict(os.environ)
p = subprocess.Popen([ os.path.join(ROOT, '.venv/bin/python'),'manage.py','importfolder',path],
    cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
ps = psutil.Process(p.pid); peak = 0; peak_single = 0; t0=time.time()
while p.poll() is None:
    try:
        procs = [ps] + ps.children(recursive=True)
        rss = [x.memory_info().rss for x in procs if x.is_running()]
        peak = max(peak, sum(rss)); peak_single = max(peak_single, max(rss))
    except psutil.Error: pass
    time.sleep(0.25)
print(json.dumps({'label':label,'seconds':round(time.time()-t0,1),'peak_tree_rss_mb':round(peak/1e6),'peak_single_process_rss_mb':round(peak_single/1e6)}))
