# Network protocol

The nodes and the Pi talk with tiny JSON messages over **UDP** on the local wifi.
Nodes **broadcast** their messages, so the Pi finds them without any IP address being
configured; the Pi remembers each sender's IP to send commands back.

## Ports

| Port | Direction | Used for |
|---|---|---|
| `8766` | nodes → Pi | Every node's status / heartbeat |
| `8767` | Pi → motor node | Motor commands |
| `8768` | Pi → traffic node | Traffic commands |
| `8769` | Pi → status display | City status for the OLED, once a second |
| `8765` | browser → Pi (HTTP) | Dashboard page, `/stream` (Server-Sent Events), `/data`, `/history` |

## Messages from the nodes (port 8766)

The Pi looks at the keys to tell the nodes apart:

| Node | Example | Recognised by |
|---|---|---|
| Traffic | `{"traffic":"auto","r":0,"y":0,"g":1,"cm":40}` (`cm` is `-1` when nothing is in range) | `traffic` |
| Motor | `{"motor":"up","left":7}` | `motor` |
| Seismic | `{"seismic":1,"vib":1,"pulses":6,"cm":36.5,"base":30.0,"move":6.5}` | `seismic` |
| Status display | `{"matrix":"ok","screen":"temp"}` | `matrix` |
| Mountain | `{"motion":0,"vibration":1,"light":72}` | none of the above |

```python
if "motor" in msg:                 apply_motor(msg, addr)
elif "traffic" in msg:             apply_traffic(msg, addr)
elif "seismic" in msg:             apply_seismic(msg, addr)
elif msg.get("matrix") == "ok":    apply_matrix(msg, addr)
else:                              apply_mountain(msg)
```

The Arduino Nano is on USB serial instead, printing lines like
`[A0] Rain  raw=812  volt=3.97V | [D4] digital=HIGH`.

## Commands from the Pi

| To | Example |
|---|---|
| Motor (8767) | `{"cmd":"up"}`, `{"cmd":"down"}`, `{"cmd":"stop"}` (sent 3 times in case one is lost) |
| Traffic (8768) | `{"mode":"manual"}`, `{"mode":"auto"}`, `{"light":"red","on":1}` |
| Status display (8769) | `{"rain":0,"temp":29.7,"gas":0,"alarm":0,"quake":0}` (`null` = unknown) |

## Dashboard HTTP endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | Dashboard page (gzip) |
| GET | `/stream` | Live updates (Server-Sent Events) |
| GET | `/data` | Current snapshot as JSON |
| GET | `/history` | 24 h hourly values + event log |
| GET | `/alarm.mp3` | Alarm sound |
| POST | `/motor` | `{"cmd":"up"|"down"|"stop"}` |
| POST | `/traffic` | Mode or single light |
| POST | `/alarm/silence` | Silence the disaster and earthquake alarms |
