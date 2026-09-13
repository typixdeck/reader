"""Original local PDF flow; use run_headless.py to avoid the physical session."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback

from pdf_fixture import write_pdf

output=Path(sys.argv[1]);output.mkdir(parents=True,exist_ok=True)
os.environ['TYPIX_READER_STATE']=str(output/'state.json')
(output/'state.json').unlink(missing_ok=True)
from typix_reader.app import APP_ID, Gdk, GLib, ReaderApplication

book=write_pdf(output/'Sea Notes.pdf')
encrypted=write_pdf(output/'locked.pdf',encrypted=True)
broken=output/'broken.pdf';broken.write_bytes(b'%PDF-1.4\nbroken')
GLib.set_prgname(APP_ID)
app=ReaderApplication();app.set_application_id('ai.typixdeck.reader.pdfqa')
step=0;started=time.monotonic();checks=[];errors=[];old=None;fit_size=None

def require(value,message):
    if not value: raise AssertionError(message)
    checks.append(message)

def capture(name):
    if os.environ.get('READER_QA_SCREENSHOT_COMMAND')=='grim':
        subprocess.run(['grim',str(output/(name+'.png'))],check=True,timeout=10)

def tick():
    global step,old,fit_size
    try:
        if time.monotonic()-started>75: raise TimeoutError(f'PDF GTK timeout step {step}')
        if not app.window:return True
        if step==0:
            app.open_path(book)
        elif step==1:
            if not app.document or app._pdf_busy or not app.pdf_image.get_pixbuf():return True
            require(app.document.kind=='pdf' and app.document.length==3,'PDF opens with three actual pages')
            require(bool(app.window.get_window().get_state()&Gdk.WindowState.FULLSCREEN),'PDF reader is fullscreen')
            require(app.content_stack.get_visible_child_name()=='pdf','PDF uses scrollable page rendering')
            fit_size=(app.pdf_image.get_pixbuf().get_width(),app.pdf_image.get_pixbuf().get_height())
            require(fit_size[1]<=app.content_stack.get_allocated_height(),'fit-page render fits available height')
            for widget in [app.pdf_fit,app.font_less,app.font_more,app.search_button,app.previous_button,app.next_button]:
                x,y=widget.translate_coordinates(app.window,0,0)
                require(x>=0 and x+widget.get_allocated_width()<=app.window.get_allocated_width(),'PDF toolbar action fits viewport')
            capture('reader-pdf-page')
            app.change_font(2)
        elif step==2:
            if app._pdf_busy:return True
            require(app.pdf_image.get_pixbuf().get_height()>fit_size[1],'zoom renders a larger PDF page')
            require(app.pdf_scroll.get_vadjustment().get_upper()>app.pdf_scroll.get_vadjustment().get_page_size(),'zoomed page scrolls')
            app.fit_pdf()
        elif step==3:
            if app._pdf_busy:return True
            require(app._pdf_zoom==1,'fit-page resets zoom')
            app.toggle_toc();app.toc_view.get_selection().select_path('2')
        elif step==4:
            if app._pdf_busy or app.position!=2:return True
            require(app.position==2,'page list jumps to selected PDF page')
            app.toggle_toc();app.show_shelf();app.open_path(book)
        elif step==5:
            if not app.document or app._pdf_busy or app.main_stack.get_visible_child_name()=='loading':return True
            require(app.position==2,'PDF page resumes through the existing shelf state')
            app.go_to(0)
        elif step==6:
            if app._pdf_busy:return True
            app.show_search();app.search_entry.set_text('lighthouse');app.search_next()
        elif step==7:
            if app._pdf_busy or app.position!=1:return True
            require(app._pdf_match is not None,'PDF search locates and highlights text on another page')
            capture('reader-pdf-search')
            app.search_next()
        elif step==8:
            if app._pdf_busy or app.position!=2:return True
            require(app.position==2,'find-next reaches the next PDF occurrence')
            app.hide_search();old=app.document;app.open_path(encrypted)
        elif step==9:
            if app.main_stack.get_visible_child_name()=='loading':return True
            require(app.document is old,'encrypted PDF failure preserves the previous book')
            require('加密' in app.info_label.get_text(),'encrypted PDF has an explicit explanation')
            app.open_path(broken)
        elif step==10:
            if app.main_stack.get_visible_child_name()=='loading':return True
            require(app.document is old,'corrupt PDF failure preserves the previous book')
            app.open_path(book);app.cancel_open()
        elif step==11:
            require(app.document is old,'cancelled PDF open preserves the previous book')
            app.close_reader();return False
        step+=1
        return True
    except Exception as exc:
        errors.append(f'step {step}: {exc}');traceback.print_exc();app.close_reader();return False

GLib.timeout_add(300,tick)
app.run(['reader-pdf-qa'])
result={'passed':not errors,'checks':checks,'errors':errors}
(output/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
print(json.dumps(result,ensure_ascii=False,indent=2))
raise SystemExit(bool(errors))
