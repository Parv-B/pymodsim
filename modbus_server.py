# modbus_server.py (Refactored for Multiple Unit IDs)
"""
Modbus TCP/IP Server Implementation for the Simulator

This module provides a Modbus TCP server that can manage multiple slave units
(Unit IDs) on a single TCP port.
"""

import logging
import threading
import time
import asyncio
from typing import Dict, Any, Optional

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
    Manages a Modbus TCP/IP server on a single port, capable of hosting
    multiple slave units (Unit IDs).
    """
    
    def __init__(self, port: int = 502):
        if not PYMODBUS_AVAILABLE:
            raise RuntimeError("Pymodbus library is not available.")

        self.port = port
        self.is_running = False
        
        # Manages the server thread and asyncio loop
        self._server_thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        
        # This will hold the data for each slave unit, keyed by unit_id
        self.slave_data: Dict[int, Dict[str, Dict]] = {}

        # The main pymodbus server context holding all slave contexts
        self._modbus_context: Optional[ModbusServerContext] = None
        
        self._sync_thread: Optional[threading.Thread] = None
        self._sync_active = False

    def add_slave_unit(self, unit_id: int):
        """Adds a new slave unit (device) to this server instance."""
        if unit_id in self.slave_data:
            log.warning(f"Unit ID {unit_id} already exists on port {self.port}")
            return

        # Create default data stores for the new unit
        self.slave_data[unit_id] = {
            'coils': {i: (i % 2 == 0) for i in range(10)},
            'discrete_inputs': {i: (i % 2 != 0) for i in range(10)},
            'holding_registers': {i: i * 10 for i in range(10)},
            'input_registers': {i: i * 100 for i in range(10)}
        }
        
        # If the server is already running, we need to dynamically add the new slave context
        if self.is_running and self._modbus_context:
            store = self._create_datastore_for_unit(unit_id)
            self._modbus_context.slaves[unit_id] = store
        
        log.info(f"Prepared Unit ID {unit_id} for port {self.port}")

    def remove_slave_unit(self, unit_id: int):
        """Removes a slave unit from this server."""
        if unit_id not in self.slave_data:
            return
        
        del self.slave_data[unit_id]
        if self.is_running and self._modbus_context and unit_id in self._modbus_context.slaves:
            del self._modbus_context.slaves[unit_id]
        
        log.info(f"Removed Unit ID {unit_id} from port {self.port}")
    
    def has_units(self) -> bool:
        """Checks if the server has any slave units left."""
        return bool(self.slave_data)

    def _create_datastore_for_unit(self, unit_id: int) -> ModbusSlaveContext:
        """Creates a Pymodbus datastore from our internal data dict for a given unit."""
        data = self.slave_data[unit_id]
        return ModbusSlaveContext(
            di=ModbusSequentialDataBlock(0, [v for k, v in sorted(data['discrete_inputs'].items())] + [False]*1000),
            co=ModbusSequentialDataBlock(0, [v for k, v in sorted(data['coils'].items())] + [False]*1000),
            hr=ModbusSequentialDataBlock(0, [v for k, v in sorted(data['holding_registers'].items())] + [0]*1000),
            ir=ModbusSequentialDataBlock(0, [v for k, v in sorted(data['input_registers'].items())] + [0]*1000),
        )

    def start(self) -> bool:
        if self.is_running:
            return False
        
        try:
            # Build the server context from all configured slave units
            slave_contexts = {
                unit_id: self._create_datastore_for_unit(unit_id) 
                for unit_id in self.slave_data
            }
            self._modbus_context = ModbusServerContext(slaves=slave_contexts, single=False)
            
            identity = ModbusDeviceIdentification()
            identity.VendorName = 'Python Modbus Gateway'
            identity.ProductCode = 'PMG'
            identity.ProductName = f'Simulated Gateway on Port {self.port}'
            
            self._server_thread = threading.Thread(
                target=self._run_server,
                args=(self._modbus_context, identity),
                daemon=True
            )
            self._server_thread.start()
            
            time.sleep(0.5)
            if not self.is_running:
                 raise RuntimeError("Server thread failed to start.")

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
        """The target method for the server thread."""
        try:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            server_coro = StartAsyncTcpServer(
                context=context,
                identity=identity,
                address=("0.0.0.0", self.port)
            )
            self.is_running = True
            log.info(f"Asyncio event loop starting for server on port {self.port}.")
            self._loop.run_until_complete(server_coro)
        except Exception as e:
            log.error(f"Modbus server on port {self.port} thread error: {e}", exc_info=True)
        finally:
            self.is_running = False
            log.info(f"Asyncio event loop stopped for server on port {self.port}.")

    def stop(self) -> bool:
        # Same stop logic as before
        if not self.is_running: return False
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
        """Syncs a single value to the pymodbus datastore for a specific unit."""
        if not self._modbus_context or not self.is_running: return
        
        # Get the context for the specific slave unit
        if unit_id not in self._modbus_context.slaves: return
        slave_context = self._modbus_context.slaves[unit_id]
        
        try:
            fc_map = {'coil': 1, 'discrete_input': 2, 'holding_register': 3, 'input_register': 4}
            val_map = {'coil': bool(value), 'discrete_input': bool(value), 'holding_register': int(value), 'input_register': int(value)}
            slave_context.setValues(fc_map[data_type], address, [val_map[data_type]])
        except Exception as e:
            log.error(f"Failed to sync to datastore for unit {unit_id} on port {self.port}: {e}")

    def _sync_monitoring_loop(self):
        """Checks for external changes for all managed units."""
        log.info(f"Sync monitoring started for server on port {self.port}.")
        while self._sync_active and self.is_running:
            time.sleep(0.5)
            if not self._modbus_context: continue
            
            for unit_id, data_stores in self.slave_data.items():
                if unit_id not in self._modbus_context.slaves: continue
                slave_context = self._modbus_context.slaves[unit_id]
                try:
                    # Check Holding Registers
                    for addr, val in data_stores['holding_registers'].items():
                        new_val = slave_context.getValues(3, addr, 1)[0]
                        if new_val != val:
                            log.info(f"External write on port {self.port}, unit {unit_id}: HR {addr} changed to {new_val}")
                            data_stores['holding_registers'][addr] = new_val
                    # Check Coils
                    for addr, val in data_stores['coils'].items():
                        new_val = slave_context.getValues(1, addr, 1)[0]
                        if new_val != val:
                            log.info(f"External write on port {self.port}, unit {unit_id}: Coil {addr} changed to {new_val}")
                            data_stores['coils'][addr] = new_val
                except Exception as e:
                    log.error(f"Error in sync loop for unit {unit_id}: {e}")
        log.info(f"Sync monitoring stopped for server on port {self.port}.")

    # --- Public Data Methods now require unit_id ---
    
    def set_data_point(self, unit_id: int, data_type: str, address: int, value: Any):
        if unit_id not in self.slave_data: return
        
        internal_store = self.slave_data[unit_id][f"{data_type}s"]
        
        if data_type in ['holding_register', 'input_register']:
            value = max(0, min(65535, int(value)))
        else:
            value = bool(value)

        internal_store[address] = value
        self._update_modbus_datastore(unit_id, data_type, address, value)

    def get_status(self) -> Dict[str, Any]:
        """Returns status for the whole server and a list of its units."""
        return {
            'port': self.port,
            'is_running': self.is_running,
            'units': list(self.slave_data.keys())
        }
    
    def get_all_data_for_unit(self, unit_id: int) -> Optional[Dict[str, Dict[int, Any]]]:
        if unit_id not in self.slave_data:
            return None
        # Return a copy to prevent direct modification
        return {
            k: v.copy() for k, v in self.slave_data[unit_id].items()
        }