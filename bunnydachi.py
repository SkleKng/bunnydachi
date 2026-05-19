#!/usr/bin/env python3
"""
Bunnydachi
  Click             : eat
  Click & drag      : walk toward cursor, settle on release point
  Double right-click: quit
"""

import asyncio, json, math, os, queue, threading, time
import tkinter as tk
from collections import deque
from PIL import Image, ImageTk
import websockets

# ── config ────────────────────────────────────────────────────────────────────
SPRITES_DIR = os.path.join(os.path.dirname(__file__), "sprites")
SERVER      = "wss://boolean-railway-consumers-rich.trycloudflare.com"
CHROMA      = "#010203"
COLS, ROWS  = 5, 5
HEIGHT      = 110
FPS         = 12
SPEED       = 13.0

CR, CG, CB = 1, 2, 3
MS = 1000 // FPS


# ── sprite loading ────────────────────────────────────────────────────────────

def _remove_bg(cell: Image.Image, thresh=220) -> Image.Image:
    """Flood-fill from every edge pixel to remove connected white background."""
    w, h = cell.size
    px = cell.load()

    def is_bg(x, y):
        r, g, b = px[x, y][:3]
        return r >= thresh and g >= thresh and b >= thresh

    visited, q = set(), deque()
    for x in range(w):
        for y in (0, h - 1):
            if is_bg(x, y) and (x, y) not in visited:
                visited.add((x, y)); q.append((x, y))
    for y in range(h):
        for x in (0, w - 1):
            if is_bg(x, y) and (x, y) not in visited:
                visited.add((x, y)); q.append((x, y))
    while q:
        x, y = q.popleft()
        px[x, y] = (CR, CG, CB, 255)
        for nx, ny in ((x+1,y),(x-1,y),(x,y+1),(x,y-1)):
            if 0 <= nx < w and 0 <= ny < h and (nx,ny) not in visited and is_bg(nx,ny):
                visited.add((nx, ny)); q.append((nx, ny))
    return cell


def load_sheet(name, tint=(255, 255, 255)):
    img = Image.open(os.path.join(SPRITES_DIR, f"{name}.png")).convert("RGBA")
    W, H = img.size
    fw, fh = W // COLS, H // ROWS

    px = img.load()
    has_alpha = any(px[x,y][3] < 10 for x,y in [(0,0),(W-1,0),(0,H-1),(W-1,H-1)])
    tr, tg, tb = tint

    fwd, rev = [], []
    for i in range(COLS * ROWS):
        col, row = i % COLS, i // COLS
        cell = img.crop((col*fw, row*fh, (col+1)*fw, (row+1)*fh)).convert("RGBA")

        if has_alpha:
            cpx = cell.load()
            cw, ch = cell.size
            for x in range(cw):
                for y in range(ch):
                    r, g, b, a = cpx[x, y]
                    if a < 128:
                        cpx[x, y] = (CR, CG, CB, 255)
                    elif (r, g, b) == (CR, CG, CB):
                        cpx[x, y] = (CR+1, CG+1, CB+1, 255)
        else:
            cell = _remove_bg(cell)

        # Multiplicative tint — white fur takes on tint colour, outlines stay dark
        if tint != (255, 255, 255):
            cpx = cell.load()
            cw, ch = cell.size
            for x in range(cw):
                for y in range(ch):
                    r, g, b, a = cpx[x, y]
                    if (r, g, b) != (CR, CG, CB):
                        cpx[x, y] = (r*tr//255, g*tg//255, b*tb//255, a)

        cw, ch  = cell.size
        scaled  = cell.resize((round(cw * HEIGHT / ch), HEIGHT), Image.NEAREST)
        flipped = scaled.transpose(Image.FLIP_LEFT_RIGHT)
        fwd.append(ImageTk.PhotoImage(scaled))
        rev.append(ImageTk.PhotoImage(flipped))

    return fwd, rev


# ── network ───────────────────────────────────────────────────────────────────

class NetworkClient:
    """Runs a websocket connection on a background asyncio thread.
    Thread-safe send() / recv_all() for the tkinter side."""

    def __init__(self, url):
        self.url       = url
        self.rx        = queue.Queue()
        self.connected = False
        self._loop     = asyncio.new_event_loop()
        self._async_tx = None   # asyncio.Queue created inside the loop
        threading.Thread(target=self._run, daemon=True).start()

    def send(self, data: dict):
        if self._async_tx and self.connected:
            self._loop.call_soon_threadsafe(self._async_tx.put_nowait, data)

    def recv_all(self) -> list:
        msgs = []
        while True:
            try:   msgs.append(self.rx.get_nowait())
            except queue.Empty: break
        return msgs

    # ── internals ─────────────────────────────────────────────────────────

    def _run(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._connect())

    async def _connect(self):
        self._async_tx = asyncio.Queue()
        try:
            async with websockets.connect(self.url) as ws:
                self.connected = True
                await asyncio.gather(self._recv(ws), self._send_loop(ws))
        except Exception as e:
            print(f"Network: {e}")
        finally:
            self.connected = False

    async def _recv(self, ws):
        async for raw in ws:
            self.rx.put(json.loads(raw))

    async def _send_loop(self, ws):
        while True:
            msg = await self._async_tx.get()
            await ws.send(json.dumps(msg))


# ── peer bunny ────────────────────────────────────────────────────────────────

class PeerBunny:
    def __init__(self, root, tint, init_state: dict):
        self.anims = {
            "idle": load_sheet("bunny_idle", tint),
            "walk": load_sheet("bunny_walk", tint),
            "eat":  load_sheet("bunny_eat",  tint),
        }

        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.attributes("-transparentcolor", CHROMA)
        self.win.configure(bg=CHROMA)
        # Peers are purely decorative — clicks fall through to whatever is below
        self.win.attributes("-disabled", True)

        first = self.anims["idle"][0][0]
        self.SW, self.SH = first.width(), first.height()
        self.canvas = tk.Canvas(self.win, width=self.SW, height=self.SH,
                                bg=CHROMA, highlightthickness=0)
        self.canvas.pack()

        self.x        = float(init_state.get("x", 0))
        self.y        = float(init_state.get("y", 0))
        self.state    = init_state.get("state", "idle")
        self.facing_r = init_state.get("facing_r", True)
        self.fi       = 0
        self._img     = None
        self.win.geometry(f"+{int(self.x)}+{int(self.y)}")
        self._tick()

    def update(self, data: dict):
        self.x        = float(data["x"])
        self.y        = float(data["y"])
        self.facing_r = data["facing_r"]
        new_state     = data["state"]
        if new_state != self.state:
            self.state = new_state
            self.fi    = 0
        self.win.geometry(f"+{int(self.x)}+{int(self.y)}")

    def destroy(self):
        self.win.destroy()

    def _tick(self):
        if not self.win.winfo_exists():
            return
        fwd, rev = self.anims.get(self.state, self.anims["idle"])
        frames = fwd if self.facing_r else rev
        n = len(frames)

        img = frames[self.fi % n]
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, anchor="nw", image=img)
        self._img = img
        self.fi += 1

        if self.state == "eat" and self.fi >= n:
            self.state = "idle"
            self.fi    = 0
        elif self.fi >= n:
            self.fi = 0

        self.win.after(MS, self._tick)


# ── local bunny ───────────────────────────────────────────────────────────────

class Bunnydachi:
    def __init__(self):
        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-transparentcolor", CHROMA)
        self.root.configure(bg=CHROMA)

        # ── connect and get server assignment ──────────────────────────────
        print(f"Connecting to {SERVER} …")
        self.net = NetworkClient(SERVER)

        my_tint, my_id, initial_peers = (255, 255, 255), "local", []
        deadline = time.time() + 3.0
        while time.time() < deadline:
            for msg in self.net.recv_all():
                if msg["type"] == "init":
                    my_id          = msg["id"]
                    my_tint        = tuple(msg["tint"])
                    initial_peers  = msg.get("peers", [])
            if my_id != "local":
                break
            time.sleep(0.05)

        if my_id == "local":
            print("No server response — running offline.")
        else:
            print(f"Assigned id={my_id}  tint={my_tint}")

        # ── load sprites with assigned tint ────────────────────────────────
        print("Loading sprites…")
        self.anims = {
            "idle": load_sheet("bunny_idle", my_tint),
            "walk": load_sheet("bunny_walk", my_tint),
            "eat":  load_sheet("bunny_eat",  my_tint),
        }
        print("Done.")

        first = self.anims["idle"][0][0]
        self.SW, self.SH = first.width(), first.height()

        self.canvas = tk.Canvas(self.root, width=self.SW, height=self.SH,
                                bg=CHROMA, highlightthickness=0)
        self.canvas.pack()

        self.SCR_W = self.root.winfo_screenwidth()
        self.SCR_H = self.root.winfo_screenheight()

        self.x = float(self.SCR_W // 2 - self.SW // 2)
        self.y = float(self.SCR_H - self.SH - 60)
        self.root.geometry(f"+{int(self.x)}+{int(self.y)}")

        self.state     = "idle"
        self.fi        = 0
        self.facing_r  = True
        self._cursor_x = self.x
        self._cursor_y = self.y
        self._press_x  = self._press_y = 0
        self._dragging = False
        self._img      = None

        # ── peers ──────────────────────────────────────────────────────────
        self.peers: dict[str, PeerBunny] = {}
        for p in initial_peers:
            self.peers[p["id"]] = PeerBunny(self.root, tuple(p["tint"]), p)

        self.canvas.bind("<ButtonPress-1>",   self._press)
        self.canvas.bind("<B1-Motion>",       self._motion)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.canvas.bind("<Double-Button-3>", lambda _: self.root.quit())

        self._tick()
        self.root.mainloop()

    # ── input ──────────────────────────────────────────────────────────────

    def _set(self, state):
        self.state = state
        self.fi    = 0

    def _press(self, e):
        self._press_x  = e.x_root
        self._press_y  = e.y_root
        self._dragging = False

    def _motion(self, e):
        if not self._dragging:
            if abs(e.x_root - self._press_x) > 5 or abs(e.y_root - self._press_y) > 5:
                self._dragging = True
                self._set("walk")
        if self._dragging:
            self._cursor_x = float(e.x_root)
            self._cursor_y = float(e.y_root)

    def _release(self, e):
        if self._dragging:
            self._dragging = False
        elif self.state != "eat":
            self._set("eat")

    # ── main loop ──────────────────────────────────────────────────────────

    def _tick(self):
        # ── render local bunny ─────────────────────────────────────────────
        fwd, rev = self.anims.get(self.state, self.anims["idle"])
        frames = fwd if self.facing_r else rev
        n = len(frames)

        img = frames[self.fi % n]
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, anchor="nw", image=img)
        self._img = img
        self.fi += 1

        if self.state == "eat":
            if self.fi >= n:
                self._set("idle")

        elif self.state == "walk":
            if self.fi >= n:
                self.fi = 0
            tx = max(0, min(self.SCR_W - self.SW, self._cursor_x - self.SW // 2))
            ty = max(0, min(self.SCR_H - self.SH, self._cursor_y - self.SH // 2))
            dx, dy = tx - self.x, ty - self.y
            dist = math.hypot(dx, dy)
            if dist > SPEED:
                self.facing_r = dx > 0
                self.x += dx / dist * SPEED
                self.y += dy / dist * SPEED
                self.root.geometry(f"+{int(self.x)}+{int(self.y)}")
            elif not self._dragging:
                self.x, self.y = tx, ty
                self.root.geometry(f"+{int(self.x)}+{int(self.y)}")
                self._set("idle")

        # ── network: send own state ────────────────────────────────────────
        self.net.send({"x": self.x, "y": self.y,
                       "state": self.state, "facing_r": self.facing_r})

        # ── network: process incoming messages ─────────────────────────────
        for msg in self.net.recv_all():
            t = msg["type"]

            if t == "peer_state":
                pid = msg["id"]
                if pid not in self.peers:
                    self.peers[pid] = PeerBunny(self.root, tuple(msg["tint"]), msg)
                else:
                    self.peers[pid].update(msg)

            elif t == "peer_left":
                pid = msg["id"]
                if pid in self.peers:
                    self.peers[pid].destroy()
                    del self.peers[pid]

        self.root.after(MS, self._tick)


if __name__ == "__main__":
    Bunnydachi()
