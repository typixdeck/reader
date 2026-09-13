"""Run one GTK check in a private Labwc session, never the physical display."""
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys

script=Path(sys.argv[1]).resolve()
session=Path(sys.argv[2]).resolve()
session.mkdir(parents=True,exist_ok=True)
runtime,config=session/'runtime',session/'config'
runtime.mkdir(mode=0o700,exist_ok=True);config.mkdir(mode=0o700,exist_ok=True)
(config/'rc.xml').write_text('<labwc_config><core><xwaylandPersistence>no</xwaylandPersistence></core></labwc_config>')
for name in ['autostart','shutdown','environment']:(config/name).write_text('')
env={key:value for key,value in os.environ.items() if key not in ['DISPLAY','WAYLAND_DISPLAY','DBUS_SESSION_BUS_ADDRESS','LABWC_PID','XDG_ACTIVATION_TOKEN']}
env.update(HOME=str(session),XDG_CONFIG_HOME=str(config),XDG_RUNTIME_DIR=str(runtime),
           LABWC_UPDATE_ACTIVATION_ENV='0',WLR_BACKENDS='headless',WLR_HEADLESS_OUTPUTS='1',
           WLR_RENDERER='pixman',GDK_BACKEND='wayland',READER_QA_SCREENSHOT_COMMAND='grim')
bootstrap=session/'run.sh'
bootstrap.write_text('#!/bin/sh\nset -eu\nwlr-randr --output HEADLESS-1 --custom-mode 800x600@60Hz\nexec '+shlex.join([sys.executable,str(script),str(session/'results')])+'\n')
command=shlex.join(['/bin/sh',str(bootstrap)])
with (session/'compositor.log').open('w') as log:
    process=subprocess.Popen(['dbus-run-session','--','labwc','-C',str(config),'-S',command],env=env,stdout=log,stderr=log,start_new_session=True)
    try:
        code=process.wait(timeout=95)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid,signal.SIGTERM)
        process.wait(timeout=5)
        raise
result=session/'results/result.json'
passed=code==0 and result.exists() and json.loads(result.read_text()).get('passed')
print(json.dumps({'passed':bool(passed),'exit':code,'result':str(result),'physical_session_accessed':False}))
raise SystemExit(not passed)
