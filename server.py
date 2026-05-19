#!/usr/bin/env python3
"""
Bunnydachi relay server.
  python server.py          — listens on 0.0.0.0:8765
  HOST / PORT env vars to override
"""

import asyncio, json, os, uuid
import websockets

HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8765"))

# Pastel tints assigned round-robin. Multiplicative blend, so white fur → tint colour.
TINTS = [
    [255, 255, 255],   # white   (player 1)
    [255, 160,  60],   # orange  (player 2)
    [255, 175, 210],   # pink    (player 3)
    [160, 255, 195],   # green   (player 4)
    [255, 245, 155],   # yellow  (player 5)
    [210, 170, 255],   # purple  (player 6)
]

clients: dict = {}   # websocket → {"id": str, "tint": list, "state": dict|None}


async def handler(ws):
    cid  = str(uuid.uuid4())[:8]
    tint = TINTS[len(clients) % len(TINTS)]
    clients[ws] = {"id": cid, "tint": tint, "state": None}
    print(f"[+] {cid}  ({len(clients)} connected)")

    # Tell the new client who they are and who is already here
    existing = [
        {"id": v["id"], "tint": v["tint"], **v["state"]}
        for w, v in clients.items()
        if w is not ws and v["state"] is not None
    ]
    await ws.send(json.dumps({"type": "init", "id": cid, "tint": tint, "peers": existing}))

    try:
        async for raw in ws:
            data = json.loads(raw)
            clients[ws]["state"] = data

            broadcast = json.dumps({"type": "peer_state", "id": cid, "tint": tint, **data})
            for w in list(clients):
                if w is not ws:
                    try:
                        await w.send(broadcast)
                    except Exception:
                        pass
    finally:
        del clients[ws]
        print(f"[-] {cid}  ({len(clients)} connected)")
        leave = json.dumps({"type": "peer_left", "id": cid})
        for w in list(clients):
            try:
                await w.send(leave)
            except Exception:
                pass


async def main():
    async with websockets.serve(handler, HOST, PORT):
        print(f"Relay running on ws://{HOST}:{PORT}")
        await asyncio.Future()   # run forever


if __name__ == "__main__":
    asyncio.run(main())
