#!/usr/bin/env python3
"""
OPC UA Server & Client GUI Application

A tkinter-based GUI with two tabs:
  - Server: Start/stop an OPC UA server and manage variables
  - Client: Connect to an OPC UA server, browse nodes, read/write values
Both tabs include real-time event loggers.

Requirements:
    pip install opcua

Usage:
    python ua_app.py
"""

import tkinter as tk
from tkinter import ttk, messagebox
import threading
import queue
import datetime
import random
import time
import math

from opcua import Server, Client, ua

# Make tabs visually distinct with bright colors
style = ttk.Style()
try:
    style.theme_use("clam")
except tk.TclError:
    pass
style.configure("TNotebook", background="#e8e8e8", borderwidth=3)
style.configure("TNotebook.Tab", background="#c0c0c0", foreground="#000000", font=("Segoe UI", 10, "bold"))
style.map("TNotebook.Tab", background=[("selected", "#4a90e2"), ("active", "#6ab0de")])


# ---------------------------------------------------------------------------
# Data type helpers
# ---------------------------------------------------------------------------

# Map UI type names to OPC UA VariantType for explicit typing.
OPCUA_TYPES = {
    "Boolean": ua.VariantType.Boolean,
    "Byte": ua.VariantType.Byte,
    "SByte": ua.VariantType.SByte,
    "Int16": ua.VariantType.Int16,
    "UInt16": ua.VariantType.UInt16,
    "Int32": ua.VariantType.Int32,
    "UInt32": ua.VariantType.UInt32,
    "Int64": ua.VariantType.Int64,
    "UInt64": ua.VariantType.UInt64,
    "Float": ua.VariantType.Float,
    "Double": ua.VariantType.Double,
    "String": ua.VariantType.String,
}

TYPE_NAMES = list(OPCUA_TYPES.keys())

# Default initial values for each type (shown when value field is blank).
DEFAULT_VALUES = {
    "Boolean": False,
    "Byte": 0, "SByte": 0,
    "Int16": 0, "UInt16": 0,
    "Int32": 0, "UInt32": 0,
    "Int64": 0, "UInt64": 0,
    "Float": 0.0, "Double": 0.0,
    "String": "",
}


def parse_value(type_name, value_str):
    """Convert a UI string into a (python_value, variant_type) tuple."""
    variant_type = OPCUA_TYPES[type_name]
    s = value_str.strip()
    if variant_type == ua.VariantType.Boolean:
        if s.lower() in ("true", "1", "yes", "t", "on"):
            return True, variant_type
        if s.lower() in ("false", "0", "no", "f", "off"):
            return False, variant_type
        raise ValueError(f"Cannot parse '{s}' as Boolean")
    if variant_type == ua.VariantType.String:
        return s, variant_type
    if variant_type in (ua.VariantType.Byte, ua.VariantType.SByte,
                        ua.VariantType.Int16, ua.VariantType.UInt16,
                        ua.VariantType.Int32, ua.VariantType.UInt32,
                        ua.VariantType.Int64, ua.VariantType.UInt64):
        return int(s), variant_type
    if variant_type in (ua.VariantType.Float, ua.VariantType.Double):
        return float(s), variant_type


# ---------------------------------------------------------------------------
# Logger Widget (thread-safe)
# ---------------------------------------------------------------------------

class LoggerWidget(ttk.LabelFrame):
    """A text-based logger that accepts messages from any thread via a queue."""

    def __init__(self, parent, title="Log"):
        super().__init__(parent, text=title)
        self._log_queue = queue.Queue()
        self._poll_id = None
        self._build_ui()
        self._poll()

    def _build_ui(self):
        toolbar = ttk.Frame(self)
        ttk.Button(toolbar, text="Clear", command=self.clear).pack(
            side=tk.LEFT, padx=(0, 5)
        )
        self.auto_scroll = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            toolbar, text="Auto-scroll", variable=self.auto_scroll
        ).pack(side=tk.LEFT)
        toolbar.pack(fill=tk.X, pady=(0, 2))

        text_frame = ttk.Frame(self)
        self.text = tk.Text(
            text_frame, height=10, state=tk.DISABLED,
            font=("Consolas", 9), wrap=tk.WORD,
        )
        # Color tags for different log levels
        self.text.tag_configure("INFO", foreground="#2e86de")
        self.text.tag_configure("ERROR", foreground="#d32f2f", font=("Consolas", 9, "bold"))
        self.text.tag_configure("WARN", foreground="#f39c12", font=("Consolas", 9, "bold"))
        self.text.tag_configure("DEBUG", foreground="#7f8c8d")
        vsb = ttk.Scrollbar(text_frame, orient=tk.VERTICAL, command=self.text.yview)
        self.text.configure(yscrollcommand=vsb.set)
        self.text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        text_frame.pack(fill=tk.BOTH, expand=True)

    def _poll(self):
        """Drain the log queue every 100 ms on the main thread."""
        try:
            while True:
                msg = self._log_queue.get_nowait()
                self.text.configure(state=tk.NORMAL)
                # Determine tag based on level in message
                tag = ""
                if "[INFO" in msg:
                    tag = "INFO"
                elif "[ERROR" in msg:
                    tag = "ERROR"
                elif "[WARN" in msg:
                    tag = "WARN"
                elif "[DEBUG" in msg:
                    tag = "DEBUG"
                end_idx = self.text.index(tk.END + "-1c")
                self.text.insert(tk.END, msg + "\n", tag)
                self.text.configure(state=tk.DISABLED)
                if self.auto_scroll.get():
                    self.text.see(tk.END)
        except queue.Empty:
            pass
        self._poll_id = self.after(100, self._poll)

    def log(self, level, message):
        """Queue a log message from any thread."""
        ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
        self._log_queue.put(f"[{ts}] [{level:>7s}] {message}")

    def clear(self):
        self.text.configure(state=tk.NORMAL)
        self.text.delete(1.0, tk.END)
        self.text.configure(state=tk.DISABLED)

    def destroy(self):
        if self._poll_id:
            self.after_cancel(self._poll_id)
        super().destroy()


# ---------------------------------------------------------------------------
# Server Tab
# ---------------------------------------------------------------------------

class ServerTab(ttk.Frame):
    """OPC UA server control panel with variable management."""

    def __init__(self, parent):
        super().__init__(parent)
        self.server = None
        self.namespace_idx = 2
        self._nodes = {}          # tree_item_id -> opcua Node
        self._refresh_id = None
        self.temp_stop = threading.Event()
        self._temp_thread = None
        self._build_ui()

    def _build_ui(self):
        # --- Server Controls ---
        ctrl = ttk.LabelFrame(self, text="Server Controls")
        ctrl.pack(fill=tk.X, padx=5, pady=5)

        ttk.Label(ctrl, text="Endpoint:").grid(
            row=0, column=0, sticky=tk.W, padx=5, pady=5
        )
        self.endpoint_var = tk.StringVar(
            value="opc.tcp://127.0.0.1:6321/freeopcua/server/"
        )
        self.endpoint_entry = ttk.Entry(ctrl, textvariable=self.endpoint_var, width=45)
        self.endpoint_entry.grid(row=0, column=1, padx=5, pady=5)

        ttk.Label(ctrl, text="Server Name:").grid(
            row=1, column=0, sticky=tk.W, padx=5, pady=5
        )
        self.server_name_var = tk.StringVar(value="My OPC UA Server")
        ttk.Entry(ctrl, textvariable=self.server_name_var, width=45).grid(
            row=1, column=1, padx=5, pady=5
        )

        self.start_btn = ttk.Button(
            ctrl, text="Start Server", command=self._start
        )
        self.start_btn.grid(row=0, column=2, padx=5, pady=5)
        self.stop_btn = ttk.Button(
            ctrl, text="Stop Server", command=self._stop, state=tk.DISABLED
        )
        self.stop_btn.grid(row=1, column=2, padx=5, pady=5)

        # --- Variable Management ---
        var_frame = ttk.LabelFrame(self, text="Variables")
        var_frame.pack(fill=tk.BOTH, padx=5, pady=5, expand=True)

        ttk.Label(var_frame, text="Name:").grid(
            row=0, column=0, padx=5, pady=2, sticky=tk.W
        )
        self.var_name_var = tk.StringVar()
        ttk.Entry(var_frame, textvariable=self.var_name_var, width=15).grid(
            row=0, column=1, padx=5, pady=2
        )

        ttk.Label(var_frame, text="Type:").grid(
            row=0, column=2, padx=5, pady=2, sticky=tk.W
        )
        self.var_type_var = tk.StringVar(value="Int32")
        type_cb = ttk.Combobox(
            var_frame, textvariable=self.var_type_var,
            values=TYPE_NAMES, width=10, state="readonly",
        )
        type_cb.grid(row=0, column=3, padx=5, pady=2)

        ttk.Label(var_frame, text="Initial Value:").grid(
            row=0, column=4, padx=5, pady=2, sticky=tk.W
        )
        self.var_value_var = tk.StringVar()
        ttk.Entry(var_frame, textvariable=self.var_value_var, width=15).grid(
            row=0, column=5, padx=5, pady=2
        )

        self.add_var_btn = ttk.Button(
            var_frame, text="Add Variable",
            command=self._add_variable, state=tk.DISABLED,
        )
        self.add_var_btn.grid(row=0, column=6, padx=5, pady=2)

        # --- Node Tree ---
        tree_frame = ttk.Frame(var_frame)
        self.node_tree = ttk.Treeview(
            tree_frame, columns=("type", "value"),
            show="tree headings", height=6,
        )
        self.node_tree.heading("#0", text="Name")
        self.node_tree.heading("type", text="Type")
        self.node_tree.heading("value", text="Value")
        self.node_tree.column("#0", width=150, minwidth=100)
        self.node_tree.column("type", width=80, minwidth=60)
        self.node_tree.column("value", width=100, minwidth=60)
        vsb = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=self.node_tree.yview)
        self.node_tree.configure(yscrollcommand=vsb.set)
        self.node_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        tree_frame.grid(
            row=1, column=0, columnspan=7, padx=5, pady=5, sticky=tk.NSEW
        )
        var_frame.rowconfigure(1, weight=1)

        # --- Logger ---
        self.logger = LoggerWidget(self, title="Server Log")
        self.logger.pack(fill=tk.BOTH, padx=5, pady=5, expand=True)

    # -- Server Lifecycle --

    def _start(self):
        endpoint = self.endpoint_var.get().strip()
        server_name = self.server_name_var.get().strip()
        if not endpoint:
            messagebox.showerror("Error", "Please enter an endpoint URL.")
            return

        self.server = Server()
        self.server.set_endpoint(endpoint)
        self.server.set_server_name(server_name)
        self.namespace_idx = self.server.register_namespace("http://my.server/")

        # Add a default temperature variable
        self.temp_node = self.server.nodes.objects.add_variable(
            self.namespace_idx, "Temperature", 20,
            varianttype=ua.VariantType.Int32,
        )
        self.temp_node.set_writable()
        self._add_node_to_tree("Temperature", self.temp_node)

        # Add more 20 data variables
        extra_vars = [
            ("Date", ua.VariantType.String, datetime.datetime.now().strftime("%Y-%m-%d")),
            ("Time", ua.VariantType.String, datetime.datetime.now().strftime("%H:%M:%S")),
            ("Second", ua.VariantType.Int32, datetime.datetime.now().second),
            ("Minute", ua.VariantType.Int32, datetime.datetime.now().minute),
            ("Hour", ua.VariantType.Int32, datetime.datetime.now().hour),
            ("DayOfWeek", ua.VariantType.Int32, datetime.datetime.now().weekday()),
            ("Month", ua.VariantType.Int32, datetime.datetime.now().month),
            ("Year", ua.VariantType.Int32, datetime.datetime.now().year),
            ("Sine", ua.VariantType.Float, 0.0),
            ("Cosine", ua.VariantType.Float, 0.0),
            ("Tangent", ua.VariantType.Float, 0.0),
            ("RandomFloat", ua.VariantType.Float, 0.0),
            ("RandomInt", ua.VariantType.Int32, 0),
            ("Pressure", ua.VariantType.Float, 101.3),
            ("Humidity", ua.VariantType.Float, 55.5),
            ("Speed", ua.VariantType.Float, 12.5),
            ("Counter", ua.VariantType.Int32, 0),
            ("Voltage", ua.VariantType.Float, 220.5),
            ("Current", ua.VariantType.Float, 3.2),
            ("Power", ua.VariantType.Float, 150.0),
        ]
        for name, vtype, init_val in extra_vars:
            node = self.server.nodes.objects.add_variable(
                self.namespace_idx, name, init_val, varianttype=vtype,
            )
            node.set_writable()
            self._add_node_to_tree(name, node)

        try:
            self.server.start()
        except Exception as exc:
            self.logger.log("ERROR", f"Failed to start server: {exc}")
            messagebox.showerror("Error", f"Failed to start server:\n{exc}")
            self._nodes.clear()
            self.node_tree.delete(*self.node_tree.get_children())
            self.server = None
            return

        self.logger.log("INFO", f"Server started at {endpoint}")
        self._set_server_running_state(True)
        self._schedule_refresh()
        self.temp_stop.clear()
        self._temp_thread = threading.Thread(target=self._update_variables, daemon=True)
        self._temp_thread.start()

    def _stop(self):
        if self.server is not None:
            try:
                self.server.stop()
                self.logger.log("INFO", "Server stopped.")
            except Exception as exc:
                self.logger.log("ERROR", f"Error stopping server: {exc}")

        self._set_server_running_state(False)
        self._cancel_refresh()
        self.temp_stop.set()
        if self._temp_thread is not None:
            self._temp_thread.join(timeout=1)
            self._temp_thread = None
        self._nodes.clear()
        self.node_tree.delete(*self.node_tree.get_children())

    def _set_server_running_state(self, running):
        self.start_btn.config(state=tk.DISABLED if running else tk.NORMAL)
        self.stop_btn.config(state=tk.NORMAL if running else tk.DISABLED)
        self.add_var_btn.config(state=tk.NORMAL if running else tk.DISABLED)
        self.endpoint_entry.config(state=tk.DISABLED if running else tk.NORMAL)

    # -- Variable Management --

    def _add_variable(self):
        name = self.var_name_var.get().strip()
        if not name:
            messagebox.showerror("Error", "Please enter a variable name.")
            return
        if self.server is None:
            messagebox.showerror("Error", "Server is not running.")
            return

        type_name = self.var_type_var.get()
        value_str = self.var_value_var.get().strip()

        try:
            if value_str:
                initial_value, variant_type = parse_value(type_name, value_str)
            else:
                initial_value = DEFAULT_VALUES[type_name]
                variant_type = OPCUA_TYPES[type_name]
        except ValueError as exc:
            messagebox.showerror("Error", f"Invalid initial value: {exc}")
            return

        try:
            node = self.server.nodes.objects.add_variable(
                self.namespace_idx, name, initial_value,
                varianttype=variant_type,
            )
            node.set_writable()
            self._add_node_to_tree(name, node)
            self.logger.log(
                "INFO",
                f"Added variable '{name}' ({type_name}) = {initial_value}",
            )
        except Exception as exc:
            self.logger.log("ERROR", f"Failed to add variable '{name}': {exc}")
            messagebox.showerror("Error", f"Failed to add variable:\n{exc}")

    def _add_node_to_tree(self, name, node):
        item_id = f"node_{name}"
        if item_id in self._nodes:
            item_id = f"node_{name}_{len(self._nodes)}"
        self._nodes[item_id] = node
        try:
            val = node.get_value()
            type_name = type(val).__name__ if val is not None else "None"
            val_str = str(val)
        except Exception:
            type_name = "N/A"
            val_str = ""
        self.node_tree.insert(
            "", "end", item_id, text=name,
            values=(type_name, val_str),
        )

    # -- Periodic Value Refresh --

    def _schedule_refresh(self):
        self._refresh_id = self.after(500, self._refresh_values)

    def _refresh_values(self):
        if self.server is None:
            return
        for item_id, node in self._nodes.items():
            try:
                val = node.get_value()
                type_name = type(val).__name__ if val is not None else "None"
                self.node_tree.item(
                    item_id, values=(type_name, str(val))
                )
            except Exception:
                pass
        self._schedule_refresh()

    def _update_variables(self):
        while not self.temp_stop.is_set():
            try:
                # Temperature
                if self.temp_node is not None:
                    self.temp_node.set_value(random.randint(-100, 500))

                # Update extra variables continuously
                t = time.time()
                for item_id, node in self._nodes.items():
                    try:
                        name = item_id.replace("node_", "")
                        if name == "Temperature":
                            continue
                        val = node.get_value()
                        if name == "Sine":
                            node.set_value(math.sin(t))
                        elif name == "Cosine":
                            node.set_value(math.cos(t))
                        elif name == "Tangent":
                            node.set_value(math.tan(t) if math.cos(t) != 0 else 0.0)
                        elif name == "Second":
                            node.set_value(datetime.datetime.now().second)
                        elif name == "Minute":
                            node.set_value(datetime.datetime.now().minute)
                        elif name == "Hour":
                            node.set_value(datetime.datetime.now().hour)
                        elif name == "DayOfWeek":
                            node.set_value(datetime.datetime.now().weekday())
                        elif name == "Month":
                            node.set_value(datetime.datetime.now().month)
                        elif name == "Year":
                            node.set_value(datetime.datetime.now().year)
                        elif name == "Date":
                            node.set_value(datetime.datetime.now().strftime("%Y-%m-%d"))
                        elif name == "Time":
                            node.set_value(datetime.datetime.now().strftime("%H:%M:%S"))
                        elif name == "RandomFloat":
                            node.set_value(random.uniform(0, 100))
                        elif name == "RandomInt":
                            node.set_value(random.randint(0, 1000))
                        elif name == "Counter":
                            current = node.get_value() if node.get_value() is not None else 0
                            node.set_value((current + 1) % 1000)
                        elif name == "Pressure":
                            node.set_value(101.3 + random.uniform(-5, 5))
                        elif name == "Humidity":
                            node.set_value(random.uniform(30, 90))
                        elif name == "Speed":
                            node.set_value(random.uniform(0, 120))
                        elif name == "Voltage":
                            node.set_value(220 + random.uniform(-10, 10))
                        elif name == "Current":
                            node.set_value(random.uniform(0, 10))
                        elif name == "Power":
                            node.set_value(random.uniform(50, 300))
                    except Exception:
                        pass
            except Exception:
                pass
            time.sleep(0.5)

    def _cancel_refresh(self):
        if self._refresh_id:
            self.after_cancel(self._refresh_id)
            self._refresh_id = None


# ---------------------------------------------------------------------------
# Client Tab
# ---------------------------------------------------------------------------

class ClientTab(ttk.Frame):
    """OPC UA client control panel with address-space browser."""

    def __init__(self, parent):
        super().__init__(parent)
        self.client = None
        self._connected = False
        self._loaded_nodes = set()
        self._build_ui()

    def _build_ui(self):
        # --- Connection Controls ---
        conn = ttk.LabelFrame(self, text="Connection")
        conn.pack(fill=tk.X, padx=5, pady=5)

        ttk.Label(conn, text="Server URL:").grid(
            row=0, column=0, padx=5, pady=5, sticky=tk.W
        )
        self.url_var = tk.StringVar(
            value="opc.tcp://127.0.0.1:4840/freeopcua/server/"
        )
        self.url_entry = ttk.Entry(conn, textvariable=self.url_var, width=45)
        self.url_entry.grid(row=0, column=1, padx=5, pady=5)

        self.connect_btn = ttk.Button(conn, text="Connect", command=self._connect)
        self.connect_btn.grid(row=0, column=2, padx=5, pady=5)
        self.disconnect_btn = ttk.Button(
            conn, text="Disconnect", command=self._disconnect, state=tk.DISABLED
        )
        self.disconnect_btn.grid(row=0, column=3, padx=5, pady=5)

        # --- Address Space Browser ---
        tree_frame = ttk.LabelFrame(self, text="Address Space")
        tree_frame.pack(fill=tk.BOTH, padx=5, pady=5, expand=True)

        self.tree = ttk.Treeview(tree_frame, show="tree", height=12)
        vsb = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        self.tree.bind("<<TreeviewOpen>>", self._on_tree_expand)
        self.tree.bind("<<TreeviewSelect>>", self._on_node_select)

        # --- Read / Write Panel ---
        rw = ttk.LabelFrame(self, text="Read / Write")
        rw.pack(fill=tk.X, padx=5, pady=5)

        ttk.Label(rw, text="Node ID:").grid(
            row=0, column=0, padx=5, pady=2, sticky=tk.W
        )
        self.node_id_var = tk.StringVar()
        ttk.Entry(
            rw, textvariable=self.node_id_var, width=35, state=tk.DISABLED
        ).grid(row=0, column=1, padx=5, pady=2)

        ttk.Label(rw, text="Value:").grid(
            row=1, column=0, padx=5, pady=2, sticky=tk.W
        )
        self.value_var = tk.StringVar()
        self.value_entry = ttk.Entry(
            rw, textvariable=self.value_var, width=35, state=tk.DISABLED
        )
        self.value_entry.grid(row=1, column=1, padx=5, pady=2)

        self.read_btn = ttk.Button(
            rw, text="Read", command=self._read_value, state=tk.DISABLED
        )
        self.read_btn.grid(row=1, column=2, padx=5, pady=2)
        self.write_btn = ttk.Button(
            rw, text="Write", command=self._write_value, state=tk.DISABLED
        )
        self.write_btn.grid(row=2, column=2, padx=5, pady=2)

        ttk.Label(rw, text="Data Type:").grid(
            row=2, column=0, padx=5, pady=2, sticky=tk.W
        )
        self.datatype_var = tk.StringVar()
        ttk.Entry(
            rw, textvariable=self.datatype_var, width=35, state=tk.DISABLED,
            foreground="gray",
        ).grid(row=2, column=1, padx=5, pady=2)

        # --- Logger ---
        self.logger = LoggerWidget(self, title="Client Log")
        self.logger.pack(fill=tk.BOTH, padx=5, pady=5, expand=True)

    # -- Connection Management --

    def _connect(self):
        url = self.url_var.get().strip()
        if not url:
            messagebox.showerror("Error", "Please enter a server URL.")
            return

        self.url_entry.config(state=tk.DISABLED)
        self.connect_btn.config(state=tk.DISABLED)

        def worker():
            try:
                self.client = Client(url, timeout=5)
                self.client.connect()
                self._connected = True
                self.logger.log("INFO", f"Connected to {url}")
                self.after(0, self._on_connected)
            except Exception as exc:
                self.logger.log("ERROR", f"Connection failed: {exc}")
                self.after(
                    0, lambda: messagebox.showerror(
                        "Error", f"Connection failed:\n{exc}"
                    )
                )
                self.after(0, self._on_connection_failed)

        threading.Thread(target=worker, daemon=True).start()

    def _on_connected(self):
        self.disconnect_btn.config(state=tk.NORMAL)
        self._browse_root()

    def _on_connection_failed(self):
        self.url_entry.config(state=tk.NORMAL)
        self.connect_btn.config(state=tk.NORMAL)

    def _disconnect(self):
        if self.client is not None:
            def worker():
                try:
                    self.client.disconnect()
                    self.logger.log("INFO", "Disconnected from server.")
                except Exception as exc:
                    self.logger.log("ERROR", f"Error disconnecting: {exc}")
                self.after(0, self._on_disconnected)
            threading.Thread(target=worker, daemon=True).start()

    def _on_disconnected(self):
        self._connected = False
        self.client = None
        self.tree.delete(*self.tree.get_children())
        self._loaded_nodes = set()
        self.disconnect_btn.config(state=tk.DISABLED)
        self.url_entry.config(state=tk.NORMAL)
        self.connect_btn.config(state=tk.NORMAL)
        self._clear_rw_panel()

    # -- Lazy Tree Browsing --

    def _browse_root(self):
        """Load the top-level Objects node into the tree."""
        if self.client is None or not self._connected:
            return
        self.tree.delete(*self.tree.get_children())
        self._loaded_nodes = set()
        try:
            objects = self.client.nodes.objects
            node_id_str = objects.nodeid.to_string()
            # Insert a dummy child so the expand arrow appears.
            self.tree.insert(node_id_str, "end", "")
            self.tree.insert("", "end", node_id_str, text="Objects")
        except Exception as exc:
            self.logger.log("ERROR", f"Failed to browse root: {exc}")

    def _on_tree_expand(self, event):
        """Lazy-load children when a tree node is expanded."""
        selected = self.tree.selection()
        if not selected:
            return
        item_id = selected[0]
        if not item_id:
            return
        if item_id in self._loaded_nodes:
            return
        children = self.tree.get_children(item_id)
        if not children:
            return  # nothing to replace (no children)
        # Remove dummy child, load real children
        self.tree.delete(*children)
        self._loaded_nodes.add(item_id)
        self._load_children(item_id)

    def _load_children(self, node_id_str):
        if self.client is None:
            return
        try:
            node = self.client.get_node(node_id_str)
            children = node.get_children()
            for child in children:
                child_id = child.nodeid.to_string()
                try:
                    browse_name = child.get_browse_name()
                    display_name = getattr(browse_name, "Name", str(child_id))
                except Exception:
                    display_name = str(child_id)
                # Insert dummy child for folders so expand arrow shows
                is_folder = self._is_folder(child)
                if is_folder:
                    self.tree.insert(child_id, "end", "")
                self.tree.insert(node_id_str, "end", child_id, text=display_name)
        except Exception as exc:
            self.logger.log("ERROR", f"Failed to load children of {node_id_str}: {exc}")

    def _is_folder(self, node):
        """Check whether an OPC UA node likely has children."""
        try:
            nc = node.get_node_class()
            return nc in (ua.NodeClass.Object, ua.NodeClass.ObjectType,
                          ua.NodeClass.VariableType, ua.NodeClass.ReferenceType,
                          ua.NodeClass.View)
        except Exception:
            return False

    # -- Read / Write --

    def _on_node_select(self, event):
        selected = self.tree.selection()
        if not selected:
            return
        item_id = selected[0]
        if not item_id or self.client is None:
            self._clear_rw_panel()
            return
        self._show_node_info(item_id)

    def _show_node_info(self, node_id_str):
        def worker():
            try:
                node = self.client.get_node(node_id_str)
                node_id = node.nodeid.to_string()
                node_class = node.get_node_class()
                is_variable = node_class == ua.NodeClass.Variable

                if is_variable:
                    datatype_node = self.client.get_node(node.get_data_type())
                    try:
                        datatype_name = datatype_node.get_browse_name().Name
                    except Exception:
                        datatype_name = datatype_node.nodeid.to_string()
                    try:
                        value = node.get_value()
                        value_str = str(value) if value is not None else ""
                    except Exception:
                        value_str = ""
                else:
                    datatype_name = node_class.name
                    value_str = ""

                self.after(0, lambda: self._display_node(
                    node_id, value_str, datatype_name, is_variable
                ))
            except Exception as exc:
                self.logger.log("ERROR", f"Failed to read node info: {exc}")
                self.after(0, self._clear_rw_panel)

        threading.Thread(target=worker, daemon=True).start()

    def _display_node(self, node_id, value_str, datatype_name, is_variable):
        self.node_id_var.set(node_id)
        self.datatype_var.set(datatype_name)
        self.value_var.set(value_str)
        if is_variable:
            self.read_btn.config(state=tk.NORMAL)
            self.write_btn.config(state=tk.NORMAL)
            self.value_entry.config(state=tk.NORMAL)
        else:
            self.read_btn.config(state=tk.DISABLED)
            self.write_btn.config(state=tk.DISABLED)
            self.value_entry.config(state=tk.DISABLED)
        self.logger.log("INFO", f"Selected node: {node_id}")

    def _read_value(self):
        selected = self.tree.selection()
        if not selected:
            return
        node_id_str = selected[0]
        if not node_id_str or self.client is None:
            return

        def worker():
            try:
                node = self.client.get_node(node_id_str)
                value = node.get_value()
                value_str = str(value) if value is not None else ""
                self.after(0, lambda: self.value_var.set(value_str))
                self.logger.log("INFO", f"Read value: {value}")
            except Exception as exc:
                self.logger.log("ERROR", f"Failed to read value: {exc}")

        threading.Thread(target=worker, daemon=True).start()

    def _write_value(self):
        selected = self.tree.selection()
        if not selected:
            return
        node_id_str = selected[0]
        if not node_id_str or self.client is None:
            return
        new_value = self.value_var.get()

        def worker():
            try:
                node = self.client.get_node(node_id_str)
                current = node.get_value()
                # Infer target type from current value's Python type
                if isinstance(current, bool):
                    # bool check must come before int (bool is subclass of int)
                    val = new_value.strip().lower() in (
                        "true", "1", "yes", "t", "on"
                    )
                    node.set_value(val)
                elif isinstance(current, int):
                    node.set_value(int(new_value))
                elif isinstance(current, float):
                    node.set_value(float(new_value))
                else:
                    node.set_value(new_value)
                self.logger.log("INFO", f"Wrote value: {new_value}")
                # Re-read to refresh display
                self.after(0, self._read_value)
            except Exception as exc:
                self.logger.log("ERROR", f"Failed to write value: {exc}")

        threading.Thread(target=worker, daemon=True).start()

    def _clear_rw_panel(self):
        self.node_id_var.set("")
        self.value_var.set("")
        self.datatype_var.set("")
        self.read_btn.config(state=tk.DISABLED)
        self.write_btn.config(state=tk.DISABLED)
        self.value_entry.config(state=tk.DISABLED)


# ---------------------------------------------------------------------------
# Main Application
# ---------------------------------------------------------------------------

class MainApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("OPC UA Server & Client")
        self.geometry("780x820")
        self._build_ui()

    def _build_ui(self):
        notebook = ttk.Notebook(self)
        notebook.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        self.server_tab = ServerTab(notebook)
        self.client_tab = ClientTab(notebook)

        notebook.add(self.server_tab, text="  Server  ")
        notebook.add(self.client_tab, text="  Client  ")

        # Ensure clean shutdown when window is closed
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _on_close(self):
        # Stop server if running
        if self.server_tab is not None:
            self.server_tab._stop()
        # Disconnect client if connected
        if self.client_tab is not None:
            if self.client_tab.client is not None:
                try:
                    self.client_tab.client.disconnect()
                except Exception:
                    pass
                self.client_tab.client = None
            self.client_tab._connected = False
        self.destroy()


if __name__ == "__main__":
    app = MainApp()
    app.mainloop()
