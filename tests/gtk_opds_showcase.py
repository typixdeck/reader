"""Clean product captures with original writing and a local demonstration feed."""
import os
import sys
import threading
import time
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

output = Path(sys.argv[1])
output.mkdir(parents=True, exist_ok=True)
os.environ["TYPIX_READER_STATE"] = str(output / "state.json")
(output / "state.json").unlink(missing_ok=True)
from typix_reader.app import APP_ID, GLib, ReaderApplication
from typix_reader.opds import Client

sea = '''# 海风与书页

清晨六点，渡口还没有醒来。沿岸的小店落着木板门，只有面包房的窗子透出暖光。我们把自行车靠在石阶旁，等第一班去岛上的船。

风从水面慢慢吹过来，带着一点盐味。我从帆布包里取出一本旧书，书页夹着昨天晒干的银杏叶。远处有人解开缆绳，金属环轻轻碰了一声。

等船的时间似乎总比别的时候宽一些。没有人催着翻页，也没有必须读完的章节。看过一段文字，就抬头看看海：云的影子沿着防波堤走，潮水把细碎的光推到脚边。

同行的朋友买来了两个温热的面包。纸袋搁在书上，我便合起它，让那个还未写完的故事也等一等。

船来了。我们沿跳板上去，选了靠窗的位置。岸边的屋顶渐渐变小，晨光落在书页上，像一条不必着急赶完的路。

# 岛上的午后

午后的岛很安静。我们穿过种着无花果的小巷，在旧邮局旁找到一张长椅。邮筒已经褪色，旁边的木牌却擦得干干净净，上面写着下一班船的时间。

一只猫沿墙头走过去，停下来观察风里的纸片。我翻到早晨折起的那一页，忽然发现，书里的街道和眼前的小巷有一点相像：都有一扇半开的窗，也都有一个愿意耐心等候的人。

# 把一天带回家

回程时太阳已经很低。海面变成柔和的铜色，船尾拖出一条长长的白线。我们没有说太多话，只把今天看到的小事记在本子的空白处。

到家后，我把银杏叶重新夹进书里。下一次打开它，也许会先想起那个渡口，以及一阵曾经替我们翻过书页的风。
'''
walk = '''# 城市漫步札记

下班后绕一点远路，是认识一座城市的好办法。沿着河走，能看到修自行车的小铺、站在门口聊天的邻居，还有每天准时亮起来的灯。

今天我记住的是一棵树。它长在两栋楼之间，枝叶把狭窄的天空分成了许多小块。有人在树下摆了一张椅子，什么也不做，只在那里坐一会儿。
'''
sea_path, walk_path = output / "海风与书页.md", output / "城市漫步札记.md"
sea_path.write_text(sea, encoding="utf-8")
walk_path.write_text(walk, encoding="utf-8")
feed = '''<feed xmlns="http://www.w3.org/2005/Atom"><title>书页之间 · 示例书库</title><link rel="search" type="application/atom+xml" href="/opds?query={searchTerms}"/>
<entry><title>海风与书页</title><author><name>Typix 原创阅读样例</name></author><link rel="http://opds-spec.org/acquisition" type="text/markdown" href="/sea.md"/></entry>
<entry><title>城市漫步札记</title><author><name>沿着河岸，认识一座城市</name></author><link rel="http://opds-spec.org/acquisition" type="text/markdown" href="/walk.md"/></entry>
<entry><title>慢下来的周末</title><author><name>从窗边的一杯茶开始</name></author><link rel="http://opds-spec.org/acquisition" type="text/plain" href="/weekend.txt"/></entry></feed>'''.encode()

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass
    def do_GET(self):
        data = sea.encode() if self.path == "/sea.md" else walk.encode() if self.path == "/walk.md" else feed
        self.send_response(200)
        self.send_header("Content-Type", "application/atom+xml")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
GLib.set_prgname(APP_ID)
app = ReaderApplication()
app.set_application_id("ai.typixdeck.reader.showcase")
step, started = 0, time.monotonic()

def capture(name):
    subprocess.run(["grim", str(output / (name + ".png"))], check=True, timeout=10)

def tick():
    global step
    if time.monotonic() - started > 35:
        app.close_reader()
        return False
    if not app.window:
        return True
    if step == 0:
        app.open_path(walk_path)
    elif step == 1:
        if not app.document:
            return True
        app.open_path(sea_path)
    elif step == 2:
        if not app.document or app.document.path != sea_path.resolve():
            return True
    elif step == 3:
        capture("reader-reading")
        app.show_shelf()
    elif step == 4:
        capture("reader-shelf")
        app.opds_client = Client(f"http://127.0.0.1:{server.server_port}/opds")
        app.show_opds()
    elif step == 5:
        if app.opds_busy or not app.opds_feed:
            return True
    elif step == 6:
        capture("reader-opds")
        app.close_reader()
        return False
    step += 1
    return True

GLib.timeout_add(600, tick)
app.run(["typix-reader-showcase"])
server.shutdown()
server.server_close()
