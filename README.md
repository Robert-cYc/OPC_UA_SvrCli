# OPC UA Server & Client GUI

A tkinter-based desktop application for running an OPC UA server and connecting to OPC UA servers as a client — all in one window with two tabs and real-time event loggers.

## Features

- **Server Tab** — Start/stop an OPC UA server, add/manage variables with custom names, types, and initial values, and see live value updates.
- **Client Tab** — Connect to any OPC UA server, browse the address space in a lazy-loading tree, read node values, and write new values.
- **Thread-safe logging** on both tabs with auto-scroll toggle and clear button.

## Requirements

- Python 3.10+ (tested with 3.14)
- `opcua` (FreeOpcUa library)

## Installation

```bash
pip install opcua
```

## Usage

### Option 1: Pre-built EXE (Windows)

A standalone executable is available in `dist/OPC_UA_ServerClient.exe`.
No Python installation required — just double-click to run.

### Option 2: Run from source

```bash
pip install opcua
python ua_app.py
```

### Server Tab

1. Enter an endpoint URL (default: `opc.tcp://0.0.0.0:4840/freeopcua/server/`).
2. Click **Start Server** — a default `Temperature` variable is created.
3. Use the **Variables** section to add more variables: enter a name, select a type, set an initial value, and click **Add Variable**.
4. The node tree below shows all created variables with their current values, refreshing automatically every 500 ms.
5. Click **Stop Server** to shut down.

### Client Tab

1. Enter the server URL (default: `opc.tcp://127.0.0.1:4840/freeopcua/server/`).
2. Click **Connect** to connect to the server.
3. Expand the **Objects** tree to browse the address space (children load lazily on expand).
4. Click any variable node to display its Node ID, data type, and current value.
5. Edit the **Value** field and click **Write** to update the node. Click **Read** to refresh.
6. Click **Disconnect** to disconnect from the server.

### Changing the Port

If port 4840 is already in use (e.g., by a system OPC UA Local Discovery Server on Windows), change the endpoint URL in the **Server** tab (e.g., `opc.tcp://0.0.0.0:4850/freeopcua/server/`) and restart.

## Supported Data Types

Boolean, Byte, SByte, Int16, UInt16, Int32, UInt32, Int64, UInt64, Float, Double, String.

## Architecture

Single-file application with four main classes:

| Class | Responsibility |
|-------|---------------|
| `LoggerWidget` | Thread-safe log display using `queue.Queue` + `after()` polling |
| `ServerTab` | OPC UA `Server` lifecycle, variable management, live value refresh |
| `ClientTab` | OPC UA `Client` connection, lazy tree browsing, read/write operations |
| `MainApp` | Root window with `ttk.Notebook` holding both tabs |

### Threading Model

- **GUI thread** — Runs tkinter's mainloop; all widget updates happen here.
- **Client worker threads** — Connection, browsing, and read/write operations run in background `threading.Thread` instances to avoid blocking the GUI. Results are posted back via `root.after(0, callback)`.
- **OPC UA library** — The `opcua` `Server` manages its own internal threads; `server.start()` and `server.stop()` return immediately.

## License

MIT
