"""Fixed-size TARS capture and a 20-minute review export. Simulation runs normally."""
import csv
import io
import json
import os
import re
import signal
import subprocess
import time
import traceback
from pathlib import Path
import numpy as np
import pytesseract
from PIL import Image
from playwright.sync_api import sync_playwright
OUT=Path('output'); OUT.mkdir(exist_ok=True)
RAW=Path('.capture'); RAW.mkdir(exist_ok=True)
WIDTH,HEIGHT=960,600
PROBE=os.getenv('TARS_PROBE','false').lower()=='true'
MINUTES=min(330,max(2,float(os.getenv('TARS_RECORD_MINUTES','330'))))
MAX_SECONDS=min(180,MINUTES*60) if PROBE else MINUTES*60
URL='https://chidera000.itch.io/tars'
STATIONS=[('WPSA',519),('DS54',539),('DS24',559),('DS34',579)]
stop_requested=False
def request_stop(*_):
    global stop_requested
    stop_requested=True
signal.signal(signal.SIGINT,request_stop)
signal.signal(signal.SIGTERM,request_stop)
def screenshot(page):
    im=Image.open(io.BytesIO(page.screenshot(timeout=30000))).convert('RGB')
    if abs(im.width-WIDTH)>1 or abs(im.height-HEIGHT)>1: raise RuntimeError('Viewport changed: '+str(im.size))
    if im.size!=(WIDTH,HEIGHT):im=im.crop((0,0,WIDTH,HEIGHT))
    return im
def ocr(im):
    return pytesseract.image_to_string(im.resize((im.width*2,im.height*2)),config='--psm 11')
def find_start(im):
    d=pytesseract.image_to_data(im.resize((WIDTH*2,HEIGHT*2)),config='--psm 11',output_type=pytesseract.Output.DICT)
    for i,w in enumerate(d['text']):
        if 'visual' in w.lower():
            return ((d['left'][i]+d['width'][i]/2)/2,(d['top'][i]+d['height'][i]/2)/2)
    return None
def prepare_menu(page):
    page.locator('canvas').first.wait_for(state='visible',timeout=180000)
    page.add_style_tag(content='html,body{margin:0!important;padding:0!important;overflow:hidden!important;width:960px!important;height:600px!important;background:#000!important}canvas,#unity-container{position:fixed!important;left:0!important;top:0!important;width:960px!important;height:600px!important;min-width:960px!important;max-width:960px!important;min-height:600px!important;max-height:600px!important;margin:0!important;padding:0!important;transform:none!important}#unity-footer{display:none!important}')
    m=page.evaluate('({width:innerWidth,height:innerHeight,outerWidth:outerWidth,outerHeight:outerHeight,x:screenX,y:screenY})')
    (OUT/'viewport.json').write_text(json.dumps(m,indent=2))
    if abs(m['width']-WIDTH)>1 or abs(m['height']-HEIGHT)>1: raise RuntimeError('Fullscreen viewport mismatch: '+str(m))
    for attempt in range(120):
        im=screenshot(page); point=find_start(im)
        if point:return im,point
        if attempt%10==0:print('Loading Unity:',ocr(im)[:200],flush=True)
        page.wait_for_timeout(2000)
    im.save(OUT/'loading-error.png')
    raise RuntimeError('Visualization of Mission was not found')
def ui_values(im):
    a=np.array(im).astype(float); states={}; samples={}
    for name,y in STATIONS:
        r,g,b=np.median(a[y-3:y+4,912:919],axis=(0,1))
        states[name]='green' if g>90 and g>1.7*r and g>1.7*b else 'red' if r>90 and r>1.7*g and r>1.7*b else 'unknown'
        samples[name]=[round(r),round(g),round(b)]
    r,g,b=a[50:480,0:800,:].transpose(2,0,1)
    blue=(b>35)&(b>r*1.15)&(b>g*1.03)
    gray=(r>55)&(abs(r-g)<15)&(abs(g-b)<15)
    digits=pytesseract.image_to_string(im.crop((907,427,951,491)).resize((176,256)),config='--psm 10 -c tessedit_char_whitelist=01234').strip()
    return {'lights':states,'rgb':samples,'green_count':sum(v=='green' for v in states.values()),'digit':digits,'blue_fraction':float(blue.mean()),'gray_fraction':float(gray.mean())}
def stop_recorder(recorder):
    if recorder and recorder.poll() is None:
        try:recorder.communicate(input=b'q',timeout=30)
        except subprocess.TimeoutExpired:
            recorder.terminate();recorder.wait(timeout=10)
def export(source,metadata):
    info=json.loads(subprocess.run(['ffprobe','-v','error','-show_entries','format=duration','-of','json',str(source)],capture_output=True,text=True,check=True).stdout)
    duration=float(info['format']['duration'])
    target=max(1,duration/2) if PROBE else min(1200,duration)
    factor=target/duration
    name='TARS_Fixed_Frame_Probe.mp4' if PROBE else 'TARS_20min_Sequence_Review.mp4'
    print('EXPORTING',json.dumps({'source_seconds':duration,'target_seconds':target,'speed':duration/target}),flush=True)
    filters=f'setpts=(PTS-STARTPTS)*{factor:.14f},fps=30,setsar=1,tpad=stop_mode=clone:stop_duration=1,trim=duration={target},setpts=PTS-STARTPTS'
    subprocess.run(['ffmpeg','-hide_banner','-loglevel','warning','-y','-i',str(source),'-vf',filters,'-an','-c:v','libx264','-threads','2','-preset','fast','-crf','22','-pix_fmt','yuv420p','-movflags','+faststart',str(OUT/name)],check=True)
    verified=json.loads(subprocess.run(['ffprobe','-v','error','-show_entries','format=duration:stream=width,height,r_frame_rate','-of','json',str(OUT/name)],capture_output=True,text=True,check=True).stdout)
    actual=float(verified['format']['duration'])
    if abs(actual-target)>0.05:raise RuntimeError('Unexpected export duration '+str(actual))
    if any(s.get('width')!=WIDTH or s.get('height')!=HEIGHT for s in verified['streams']):raise RuntimeError('Unexpected export dimensions')
    metadata.update({'output_file':name,'source_duration_seconds':duration,'output_duration_seconds':actual,'playback_speed':duration/target,'export_verified':True})
    (OUT/'capture.json').write_text(json.dumps(metadata,indent=2))
def run():
    metadata={'source':URL,'mode':'probe' if PROBE else 'long','capture_limit_seconds':MAX_SECONDS,'width':WIDTH,'height':HEIGHT,'source_fps':5,'target_seconds':1200,'complete':False,'verification':'Requires visual verification of Earth-Moon-Earth sequence','recording_method':'Fixed X11 screen region; viewport screenshots for evidence only'}
    recorder=None;started=None;source=RAW/'source.mkv';observations=[];browser=None;failure=None
    log=open(OUT/'browser-log.txt','w',buffering=1);ff_log=open(OUT/'ffmpeg-capture.txt','w')
    csv_file=open(OUT/'ui-observations.csv','w',newline='',buffering=1)
    writer=csv.DictWriter(csv_file,fieldnames=['elapsed_seconds','WPSA','DS54','DS24','DS34','green_count','displayed_count','blue_fraction','gray_fraction','text']);writer.writeheader()
    with sync_playwright() as p:
        try:
            browser=p.chromium.launch(headless=False,args=['--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader','--ignore-gpu-blocklist','--disable-dev-shm-usage','--autoplay-policy=no-user-gesture-required','--disable-background-timer-throttling','--disable-renderer-backgrounding','--disable-backgrounding-occluded-windows','--force-device-scale-factor=1','--window-position=0,0','--window-size=960,600','--start-fullscreen','--kiosk'])
            context=browser.new_context(no_viewport=True);page=context.new_page()
            def console(m):
                if re.fullmatch(r'[\d.\-+eE ]+',m.text.strip()):return
                log.write(json.dumps({'elapsed_seconds':None if started is None else round(time.monotonic()-started,2),'type':m.type,'text':m.text})+'\n')
            page.on('console',console);page.on('pageerror',lambda e:log.write('PAGEERROR '+str(e)+'\n'))
            page.goto(URL,wait_until='domcontentloaded',timeout=120000)
            button=page.get_by_text('Run game',exact=True)
            if button.count():button.click()
            source_url=None
            for _ in range(60):
                source_url=next((f.url for f in page.frames if 'html-classic.itch.zone' in f.url),None)
                if source_url:break
                page.wait_for_timeout(1000)
            if not source_url:raise RuntimeError('Unity iframe did not load')
            page.goto(source_url,wait_until='domcontentloaded',timeout=120000)
            cdp=context.new_cdp_session(page);window=cdp.send('Browser.getWindowForTarget')
            cdp.send('Browser.setWindowBounds',{'windowId':window['windowId'],'bounds':{'windowState':'fullscreen'}})
            menu,point=prepare_menu(page);menu.save(OUT/'menu.png')
            page.mouse.click(*point);page.wait_for_timeout(2500);page.mouse.click(900,24);page.wait_for_timeout(500)
            legend=screenshot(page);legend.save(OUT/'legend.png');(OUT/'legend-text.txt').write_text(ocr(legend))
            page.reload(wait_until='domcontentloaded',timeout=120000);menu,point=prepare_menu(page)
            recorder=subprocess.Popen(['ffmpeg','-hide_banner','-loglevel','warning','-y','-f','x11grab','-framerate','5','-video_size','960x600','-i',os.environ['DISPLAY']+'+0,0','-an','-c:v','libx264','-threads','1','-preset','ultrafast','-crf','25','-pix_fmt','yuv420p',str(source)],stdin=subprocess.PIPE,stdout=ff_log,stderr=ff_log)
            page.wait_for_timeout(400);page.mouse.click(*point);started=time.monotonic()
            print('MISSION STARTED: fixed 960x600 capture',flush=True)
            next_sample=0;moon_run=0;return_run=0;return_at=None;prior_lights=None
            while not stop_requested and time.monotonic()-started<MAX_SECONDS:
                if recorder.poll() is not None:raise RuntimeError('Screen recorder stopped unexpectedly')
                elapsed=time.monotonic()-started
                if elapsed>=next_sample:
                    im=screenshot(page);text=ocr(im);values=ui_values(im)
                    observations.append({'elapsed_seconds':round(elapsed,2),**values,'text':text})
                    writer.writerow({'elapsed_seconds':round(elapsed,2),**values['lights'],'green_count':values['green_count'],'displayed_count':values['digit'],'blue_fraction':round(values['blue_fraction'],5),'gray_fraction':round(values['gray_fraction'],5),'text':text.replace('\n',' | ')})
                    lights=tuple(values['lights'].values())
                    if elapsed<5 or lights!=prior_lights or int(elapsed)//300!=int(max(0,elapsed-30))//300:im.save(OUT/f'frame-{int(elapsed):06d}.png')
                    if len(list(OUT.glob('frame-*.png')))>120:sorted(OUT.glob('frame-*.png'))[1].unlink()
                    prior_lights=lights;normal=re.sub('[^a-z ]',' ',text.lower())
                    moon_text='moon' in normal and any(s in normal for s in ['flyby','lunar','orbiting moon'])
                    moon_visual=values['blue_fraction']<0.01 and values['gray_fraction']>0.12
                    moon_run=moon_run+1 if moon_text or moon_visual else 0
                    if moon_run>=3:
                        metadata.setdefault('lunar_candidate_seconds',elapsed);im.save(OUT/'lunar-candidate.png')
                    earth_return=metadata.get('lunar_candidate_seconds') is not None and values['blue_fraction']>0.08
                    return_run=return_run+1 if earth_return else 0
                    if return_run>=3 and return_at is None:
                        return_at=elapsed;metadata['earth_return_candidate_seconds']=elapsed;im.save(OUT/'return-candidate.png')
                    explicit_end=any(s in normal for s in ['mission complete','simulation complete','mission ended'])
                    if not PROBE and return_at is not None and elapsed-return_at>180:
                        metadata['end_reason']='Lunar and Earth-return visual candidates; requires manual verification';break
                    if not PROBE and explicit_end and metadata.get('lunar_candidate_seconds') is not None:
                        metadata['end_reason']='Completion message after lunar candidate; requires manual verification';break
                    print('CAPTURE',json.dumps({'seconds':round(elapsed),'lights':values['lights'],'count':values['digit'],'blue':round(values['blue_fraction'],3),'gray':round(values['gray_fraction'],3)}),flush=True)
                    next_sample=elapsed+(10 if PROBE else 30);(OUT/'capture.json').write_text(json.dumps(metadata,indent=2))
                page.wait_for_timeout(1000)
            metadata['recorded_seconds']=round(time.monotonic()-started,2);metadata.setdefault('end_reason','Capture limit reached; complete sequence is not yet verified')
            screenshot(page).save(OUT/'final.png')
        except BaseException as e:
            failure=e;metadata['error']=str(e);(OUT/'error.txt').write_text(traceback.format_exc())
            if started:metadata['recorded_seconds']=round(time.monotonic()-started,2)
        finally:
            stop_recorder(recorder)
            if browser:browser.close()
            log.close();ff_log.close();csv_file.close()
            (OUT/'observations.json').write_text(json.dumps(observations,indent=2));(OUT/'capture.json').write_text(json.dumps(metadata,indent=2))
    if source.exists() and source.stat().st_size>10000:export(source,metadata)
    if failure:raise failure
    print('FINISHED',json.dumps(metadata),flush=True)
if __name__=='__main__':run()
