"""Explicit live test on the current user's two emulator desktops; never changes settings."""
from concurrent.futures import ThreadPoolExecutor
import ctypes as C
from ctypes import wintypes as W
from datetime import datetime, timezone
import json
from pathlib import Path
import statistics
import time
import argparse

from emulator_agent.controller import Controller
from emulator_agent.discovery import ControlError
from emulator_agent.win32 import desktop_context, signature, user


def cover(instance):
    signature(user,"GetWindowRect",W.BOOL,W.HWND,C.POINTER(W.RECT))
    signature(user,"CreateWindowExW",W.HWND,W.DWORD,W.LPCWSTR,W.LPCWSTR,W.DWORD,
              C.c_int,C.c_int,C.c_int,C.c_int,W.HWND,W.HMENU,W.HINSTANCE,W.LPVOID)
    signature(user,"DestroyWindow",W.BOOL,W.HWND)
    rect=W.RECT()
    if not user.GetWindowRect(instance.hwnd,C.byref(rect)):
        raise RuntimeError("Cannot get emulator window bounds")
    hwnd=user.CreateWindowExW(0x08000008,"STATIC","Emulator Agent validation",0x90000000,
        rect.left,rect.top,rect.right-rect.left,rect.bottom-rect.top,None,None,None,None)
    if not hwnd:raise RuntimeError("Cannot create non-activating cover window")
    return hwnd


def main(duration):
    root=Path(__file__).resolve().parent.parent
    output=root/'runtime/validation'
    output.mkdir(parents=True,exist_ok=True)
    c=Controller(root)
    targets=['ld:0','mumu:0']
    report={'started_at':datetime.now(timezone.utc).isoformat(),'checks':{},'samples':{},'errors':[]}
    contexts=[]
    try:
        for name in targets:
            assert c.registry.get(name).running,f'{name} must already be running'
            c.action(name,'press_key',key='home')
        time.sleep(.5)
        for name in targets:
            inst=c.registry.get(name)
            baseline=desktop_context()
            hwnd=cover(inst)
            try:
                frame=c.screenshot(name)
                (output/f'{name[:2]}-covered.png').write_bytes(frame['image_bytes'])
                # A blank middle area on the verified local launcher layouts.
                result=c.action(name,'long_press',frame_id=frame['frame_id'],points=[[frame['width']//2,frame['height']//2]],duration=.6)
                (output/f'{name[:2]}-covered-long-press.png').write_bytes(result['image_bytes'])
                contexts.append(baseline==desktop_context())
            finally:user.DestroyWindow(hwnd)
            c.action(name,'press_key',key='home')
        report['checks']['opaque_cover_screenshot_and_long_press']=True
        report['checks']['foreground_and_cursor_unchanged']=all(contexts)
        assert all(contexts),'Desktop changed during controlled cover check'

        for name in targets:
            frame=c.screenshot(name,max_size=800,crop=[0,0,300,400])
            assert frame['width']==300 and frame['height']==400
        report['checks']['crop_and_image_metadata']=True

        for name in targets:
            frame=c.screenshot(name)
            started=time.monotonic()
            with ThreadPoolExecutor() as pool:
                future=pool.submit(c.action,name,'long_press',frame_id=frame['frame_id'],
                                   points=[[frame['width']//2,frame['height']//2]],duration=5)
                for _ in range(100):
                    if c.slot(name).worker.active.is_set():break
                    time.sleep(.01)
                time.sleep(.3)
                c.cancel(name)
                try:
                    future.result()
                except ControlError as exc:
                    assert '取消' in str(exc),str(exc)
                else:raise AssertionError('Cancellation ignored')
            assert time.monotonic()-started<3
            c.action(name,'press_key',key='home')
        report['checks']['long_press_cancel_and_release']=True

        before=c.screenshot('mumu:0')
        old=c.slot('mumu:0').worker
        old.process.terminate()
        old.process.wait(timeout=5)
        c.screenshot('mumu:0')
        try:c.action('mumu:0','click',frame_id=before['frame_id'],points=[[1,1]])
        except ControlError:pass
        else:raise AssertionError('Old frame accepted after worker restart')
        report['checks']['owned_worker_crash_recovery_and_stale_frame']=True

        started=time.monotonic()
        samples={name:[] for name in targets}
        cycles=0
        with ThreadPoolExecutor(max_workers=2) as pool:
            while True:
                def observe(name):
                    begin=time.monotonic()
                    frame=c.screenshot(name)
                    assert frame['image_bytes'].startswith(b'\x89PNG')
                    if cycles%4==0:c.action(name,'press_key',key='home')
                    return time.monotonic()-begin
                futures={name:pool.submit(observe,name) for name in targets}
                for name,future in futures.items():samples[name].append(future.result())
                cycles+=1
                elapsed=time.monotonic()-started
                print(json.dumps({'elapsed_seconds':round(elapsed),'cycles':cycles}),flush=True)
                if elapsed>=duration:break
                time.sleep(min(10,duration-elapsed))
        report['continuous_seconds']=round(time.monotonic()-started,2)
        for name,values in samples.items():
            ordered=sorted(values)
            report['samples'][name]={'count':len(values),'median_ms':round(statistics.median(values)*1000),
                'p95_ms':round(ordered[min(len(ordered)-1,int(len(ordered)*.95))]*1000)}
        report['status']='passed'
    except Exception as exc:
        report['status']='failed'
        report['errors'].append(str(exc))
        raise
    finally:
        c.close()
        report['finished_at']=datetime.now(timezone.utc).isoformat()
        (output/'live-results.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--duration',type=int,default=0)
    args=parser.parse_args()
    main(args.duration)
