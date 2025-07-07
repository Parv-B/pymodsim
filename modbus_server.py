# modbus_server.py (Corrected for Context API)
"""
Modbus TCP/IP Server Implementation for the Simulator

This module provides a Modbus TCP server process that can host multiple slave
units, each with its own data store.
"""

import logging
import threading
import time
import asyncio
from typing import Dict, Any, Optional

# Check for pymodbus and handle potential import errors
try:
    from pymodbus.server import StartAsyncTcpServer
    from pymodbus.device import ModbusDeviceIdentification
    from pymodbus.datastore import ModbusSequentialDataBlock, ModbusSlaveContext, ModbusServerContext
    PYMODBUS_AVAILABLE = True
except ImportError:
    print("FATAL: pymodbus is not installed. Please run 'pip install pymodbus'")
    PYMODBUS_AVAILABLE = False


log = logging.getLogger(__name__)


class ModbusServer:
    """
    Manages a single Modbus TCP/IP server process that can host multiple slave units.
    """
    
    def __init__(self, port: int, name: str = "Modbus Server"):
        if not PYMODBUS_AVAILABLE:
            raise RuntimeError("Pymodbus library is not available.")

        self.port = port
        self.name = f"{name} on Port {port}"
        self.is_running = False
        
        self._server_thread: Optional[threading.Thread] = None
        self._server_task: Optional[asyncio.Task] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        
        self.slave_data: Dict[int, Dict[str, Dict[int, Any]]] = {}
        
        self._modbus_context: Optional[ModbusServerContext] = None
        
        self._sync_thread: Optional[threading.Thread] = None
        self._sync_active = False

    def _create_store_for_unit(self, unit_id: int) -> Optional[ModbusSlaveContext]:
        """Creates a ModbusSlaveContext from our internal data for a specific unit."""
        unit_data = self.slave_data.get(unit_id)
        if not unit_data:
            return None
        
        def get_values(data_dict, default_val, padding=1000):
            if not data_dict:
                return [default_val] * padding
            max_addr = max(data_dict.keys()) if data_dict else -1
            val_list = [default_val] * (max_addr + 1)
            for addr, val in data_dict.items():
                val_list[addr] = val
            return val_list + [default_val] * padding

        return ModbusSlaveContext(
            di=ModbusSequentialDataBlock(0, get_values(unit_data['discrete_inputs'], False)),
            co=ModbusSequentialDataBlock(0, get_values(unit_data['coils'], False)),
            hr=ModbusSequentialDataBlock(0, get_values(unit_data['holding_registers'], 0)),
            ir=ModbusSequentialDataBlock(0, get_values(unit_data['input_registers'], 0)),
            zero_mode=True
        )

    # --- Methods required by app.py ---

    def add_slave_unit(self, unit_id: int):
        """Adds a new slave unit with default data points."""
        if unit_id in self.slave_data:
            log.warning(f"Unit ID {unit_id} already exists on server port {self.port}.")
            return

        self.slave_data[unit_id] = {
            'coils': {i: (i % 2 == 0) for i in range(10)},
            'discrete_inputs': {i: (i % 2 != 0) for i in range(10)},
            'holding_registers': {i: i * 10 for i in range(10)},
            'input_registers': {i: i * 100 for i in range(10)},
        }
        log.info(f"Prepared new slave unit {unit_id} for server on port {self.port}")

        if self.is_running and self._modbus_context:
            new_store = self._create_store_for_unit(unit_id)
            if new_store:
                # --- THIS IS THE CORRECTED LINE ---
                # Use item assignment directly on the context object
                self._modbus_context[unit_id] = new_store
                log.info(f"Dynamically added unit {unit_id} to running server on port {self.port}")

    def remove_slave_unit(self, unit_id: int):
        """Removes a slave unit."""
        if unit_id not in self.slave_data:
            return
        del self.slave_data[unit_id]
        log.info(f"Removed slave data for unit {unit_id} from server on port {self.port}")

        # --- THIS IS THE CORRECTED BLOCK ---
        # Check for existence and delete the item directly on the context object
        if self.is_running and self._modbus_context and unit_id in self._modbus_context:
            del self._modbus_context[unit_id]
            log.info(f"Dynamically removed unit {unit_id} from running server context.")

    def has_units(self) -> bool:
        """Checks if the server has any slave units configured."""
        return bool(self.slave_data)

    def get_all_data_for_unit(self, unit_id: int) -> Optional[Dict[str, Dict[int, Any]]]:
        """Returns all current data points for a specific unit."""
        return self.slave_data.get(unit_id)

    def set_data_point(self, unit_id: int, data_type: str, address: int, value: Any):
        """Updates or adds a data point for a specific slave unit."""
        unit_data = self.slave_data.get(unit_id)
        if not unit_data:
            log.warning(f"Attempted to set data for non-existent unit {unit_id} on port {self.port}")
            return

        key_map = {
            'coil': 'coils', 'discrete_input': 'discrete_inputs',
            'holding_register': 'holding_registers', 'input_register': 'input_registers'
        }
        data_key = key_map.get(data_type)

        if not data_key:
            log.error(f"Invalid data type '{data_type}' for set_data_point")
            return

        sanitized_value = bool(value) if data_key in ['coils', 'discrete_inputs'] else max(0, min(65535, int(value)))
        unit_data[data_key][address] = sanitized_value
        self._update_modbus_datastore(unit_id, data_type, address, sanitized_value)
    
    # --- Core Server Logic ---

    def start(self) -> bool:
        if self.is_running:
            return False
        if not self.has_units():
            log.error(f"Cannot start server on port {self.port}: No slave units have been added.")
            return False
        try:
            slaves_context = {uid: self._create_store_for_unit(uid) for uid in self.slave_data.keys()}
            self._modbus_context = ModbusServerContext(slaves=slaves_context, single=False)
            
            identity = ModbusDeviceIdentification()
            identity.VendorName = 'Python Modbus Sim'
            identity.ProductCode = 'PMS'
            identity.ProductName = f'Simulated Server - Port {self.port}'
            identity.ModelName = 'Python Server'
            identity.MajorMinorRevision = '1.0'
            
            self._server_thread = threading.Thread(target=self._run_server, args=(self._modbus_context, identity), daemon=True)
            self._server_thread.start()
            time.sleep(0.5)
            
            if not self.is_running:
                 raise RuntimeError("Server thread failed to start the event loop.")

            self._sync_active = True
            self._sync_thread = threading.Thread(target=self._sync_monitoring_loop, daemon=True)
            self._sync_thread.start()
            
            log.info(f"Modbus server started successfully on port {self.port}")
            return True
        except Exception as e:
            log.error(f"Failed to start Modbus server on port {self.port}: {e}", exc_info=True)
            self.is_running = False
            return False

    def _run_server(self, context, identity):
        """The target method for the server thread. Runs the asyncio event loop."""
        try:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            server_coro = StartAsyncTcpServer(context=context, identity=identity, address=("0.0.0.0", self.port))
            self._server_task = self._loop.create_task(server_coro)
            self.is_running = True
            log.info(f"Asyncio event loop starting for server on port {self.port}.")
            self._loop.run_forever()
        except Exception as e:
            log.error(f"Modbus server thread error on port {self.port}: {e}", exc_info=True)
        finally:
            if self._loop and self._loop.is_running():
                self._loop.stop()
            self.is_running = False
            log.info(f"Asyncio event loop stopped for server on port {self.port}.")

    def stop(self) -> bool:
        if not self.is_running:
            return False
        try:
            self._sync_active = False
            if self._sync_thread and self._sync_thread.is_alive():
                self._sync_thread.join(timeout=1.0)
            if self._loop and self._loop.is_running():
                self._loop.call_soon_threadsafe(self._loop.stop)
            if self._server_thread and self._server_thread.is_alive():
                self._server_thread.join(timeout=2.0)
            self.is_running = False
            log.info(f"Modbus server on port {self.port} stopped.")
            return True
        except Exception as e:
            log.error(f"Error stopping Modbus server on port {self.port}: {e}", exc_info=True)
            return False

    def _update_modbus_datastore(self, unit_id: int, data_type: str, address: int, value: Any):
        """Syncs a single value from our internal dict to the pymodbus datastore."""
        if not self._modbus_context or not self.is_running or unit_id not in self._modbus_context:
            return
        
        slave_context = self._modbus_context[unit_id]
        try:
            fc_map = {'coil': 1, 'discrete_input': 2, 'holding_register': 3, 'input_register': 4}
            slave_context.setValues(fc_map[data_type], address, [value])
        except Exception as e:
            log.error(f"Failed to sync to datastore for port {self.port} unit {unit_id}: {e}")

    def _sync_monitoring_loop(self):
        """Periodically checks the pymodbus datastore for changes and updates internal dicts."""
        log.info(f"Sync monitoring started for server on port {self.port}.")
        while self._sync_active and self.is_running:
            time.sleep(0.5)
            try:
                if self._modbus_context:
                    for unit_id, unit_data in list(self.slave_data.items()):
                        if unit_id not in self._modbus_context: continue
                        
                        slave_context = self._modbus_context[unit_id]
                        # Check Holding Registers & Coils for external changes
                        for addr, val in list(unit_data['holding_registers'].items()):
                            if (new_val := slave_context.getValues(3, addr, 1)[0]) != val:
                                log.info(f"External write on port {self.port} unit {unit_id}: HR {addr} changed from {val} to {new_val}")
                                unit_data['holding_registers'][addr] = new_val
                        for addr, val in list(unit_data['coils'].items()):
                            if (new_val := slave_context.getValues(1, addr, 1)[0]) != val:
                                log.info(f"External write on port {self.port} unit {unit_id}: Coil {addr} changed from {val} to {new_val}")
                                unit_data['coils'][addr] = new_val
            except Exception as e:
                log.error(f"Error in sync monitoring loop for port {self.port}: {e}", exc_info=True)
                time.sleep(2.0)
        log.info(f"Sync monitoring stopped for server on port {self.port}.")