
#!/usr/bin/env python3
import subprocess,time,os,shutil
from collections import deque

SAMPLE=5
HIST=30
hist=deque(maxlen=HIST)

def sh(cmd):
    return subprocess.check_output(cmd,shell=True,text=True,stderr=subprocess.DEVNULL)

def battery():
    bat=sh("upower -e | grep BAT").strip()
    txt=sh(f'upower -i "{bat}"')
    d={}
    for l in txt.splitlines():
        if ":" in l:
            k,v=l.split(":",1)
            d[k.strip()]=v.strip()
    return {
      "power":d.get("energy-rate","? W"),
      "pct":d.get("percentage","?"),
      "state":d.get("state","?"),
      "left":d.get("time to empty") or d.get("time to full") or "-"
    }

def cpu():
    with open("/proc/stat") as f:
        a=list(map(int,f.readline().split()[1:]))
    t1=sum(a); i1=a[3]+a[4]
    time.sleep(0.15)
    with open("/proc/stat") as f:
        b=list(map(int,f.readline().split()[1:]))
    t2=sum(b); i2=b[3]+b[4]
    return round((1-(i2-i1)/(t2-t1))*100,1)

def ram():
    vals={}
    with open("/proc/meminfo") as f:
        for l in f:
            k,v=l.split(":")
            vals[k]=int(v.split()[0])
    return round((1-vals["MemAvailable"]/vals["MemTotal"])*100,1)

while True:
    b=battery()
    cp=cpu()
    rm=ram()
    p=float(b["power"].split()[0]) if b["power"][0].isdigit() else 0
    hist.append(p)
    os.system("clear")
    print("Battery Monitor (Ctrl+C to exit)\n")
    if hist:
        h=10
        mx=max(max(hist),1); mn=min(hist)
        rng=max(mx-mn,0.5)
        for row in range(h,0,-1):
            lvl=mn+rng*row/h
            line=""
            for v in hist:
                line+="█" if v>=lvl else " "
            print(f"{lvl:4.1f}│{line}")
        print("    └"+"─"*len(hist))
    print()
    print(f"{'Time':8} {'Power':>7} {'CPU':>6} {'RAM':>6} {'Batt':>6} {'Left':>10} {'State'}")
    print("-"*65)
    print(f"{time.strftime('%H:%M:%S'):8} {b['power']:>7} {str(cp)+'%':>6} {str(rm)+'%':>6} {b['pct']:>6} {b['left']:>10} {b['state']}")
    print(f"\nRefresh every {SAMPLE}s")
    time.sleep(max(0,SAMPLE-0.15))
