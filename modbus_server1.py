# modbus_server.py (Corrected Version)
"""
Modbus TCP/IP Server Implementation for the Simulator

This module provides a Modbus TCP slave simulation with configurable data points
and support for common function codes. It's designed to run in a separate
thread and be managed by a parent application.
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
    Manages a single Modbus TCP/IP slave instance.
    
    This class handles the startup, shutdown, and data management for one
    Modbus slave, designed to run in its own thread.
    """
    
    def __init__(self, unit_id: int = 1, port: int = 502, name: str = "Modbus Slave"):
        if not PYMODBUS_AVAILABLE:
            raise RuntimeError("Pymodbus library is not available.")

        self.unit_id = unit_id
        self.port = port
        self.name = name
        self.is_running = False
        
        # Threading and asyncio event loop management
        self._server_thread: Optional[threading.Thread] = None
        self._server_task: Optional[asyncio.Task] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        
        # Internal data storage (the "source of truth" for the UI)
        self.coils: Dict[int, bool] = {i: (i % 2 == 0) for i in range(10)}
        self.discrete_inputs: Dict[int, bool] = {i: (i % 2 != 0) for i in range(10)}
        self.holding_registers: Dict[int, int] = {i: i * 10 for i in range(10)}
        self.input_registers: Dict[int, int] = {i: i * 100 for i in range(10)}
        
        # Pymodbus data context for the actual server
        self._modbus_context: Optional[ModbusServerContext] = None
        
        # Sync monitoring for external changes
        self._sync_thread: Optional[threading.Thread] = None
        self._sync_active = False

    def start(self) -> bool:
        if self.is_running:
            log.warning(f"Modbus slave '{self.name}' is already running.")
            return False
            
        try:
            # Initialize Modbus datastore with a large enough default size
            # This is where the Modbus master will read/write data.
            store = ModbusSlaveContext(
                di=ModbusSequentialDataBlock(0, [v for k, v in sorted(self.discrete_inputs.items())] + [False]*1000),
                co=ModbusSequentialDataBlock(0, [v for k, v in sorted(self.coils.items())] + [False]*1000),
                hr=ModbusSequentialDataBlock(0, [v for k, v in sorted(self.holding_registers.items())] + [0]*1000),
                ir=ModbusSequentialDataBlock(0, [v for k, v in sorted(self.input_registers.items())] + [0]*1000),
            )
            self._modbus_context = ModbusServerContext(slaves={self.unit_id: store}, single=False)
            
            # --- THIS IS THE CORRECTED SECTION ---
            # Device identification, created in a way that is compatible with older pymodbus versions
            identity = ModbusDeviceIdentification()
            identity.VendorName = 'Python Modbus Sim'
            identity.ProductCode = 'PMS'
            identity.ProductName = f'Simulated Slave - {self.name}'
            identity.ModelName = 'Python Slave'
            identity.MajorMinorRevision = '1.0'
            # --- END OF CORRECTION ---
            
            # Run the server in a separate thread
            self._server_thread = threading.Thread(
                target=self._run_server,
                args=(self._modbus_context, identity),
                daemon=True
            )
            self._server_thread.start()
            
            # Allow a moment for the server thread to initialize
            time.sleep(0.5)
            
            if not self.is_running:
                 raise RuntimeError("Server thread failed to start the event loop.")

            # Start the sync monitoring thread
            self._sync_active = True
            self._sync_thread = threading.Thread(target=self._sync_monitoring_loop, daemon=True)
            self._sync_thread.start()
            
            log.info(f"Modbus slave '{self.name}' started successfully on port {self.port}")
            return True
            
        except Exception as e:
            log.error(f"Failed to start Modbus slave '{self.name}': {e}", exc_info=True)
            self.is_running = False
            return False
    
    def _run_server(self, context, identity):
        """The target method for the server thread. Runs the asyncio event loop."""
        try:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)

            # Create the server coroutine
            server_coro = StartAsyncTcpServer(
                context=context,
                identity=identity,
                address=("0.0.0.0", self.port)
            )

            # Schedule the server to run
            self._server_task = self._loop.create_task(server_coro)
            self.is_running = True
            log.info(f"Asyncio event loop starting for slave '{self.name}'.")
            self._loop.run_forever()

        except Exception as e:
            # Port in use is a common error here
            log.error(f"Modbus slave '{self.name}' server thread error: {e}", exc_info=True)
        finally:
            if self._loop and self._loop.is_running():
                self._loop.stop()
            self.is_running = False
            log.info(f"Asyncio event loop stopped for slave '{self.name}'.")

    def stop(self) -> bool:
        if not self.is_running:
            log.warning(f"Modbus slave '{self.name}' is not running.")
            return False
            
        try:
            # Stop sync monitoring first
            self._sync_active = False
            if self._sync_thread and self._sync_thread.is_alive():
                self._sync_thread.join(timeout=1.0)

            # Gracefully stop the asyncio event loop from another thread
            if self._loop and self._loop.is_running():
                log.info(f"Requesting event loop stop for slave '{self.name}'...")
                self._loop.call_soon_threadsafe(self._loop.stop)
            
            if self._server_thread and self._server_thread.is_alive():
                self._server_thread.join(timeout=2.0)

            self.is_running = False
            log.info(f"Modbus slave '{self.name}' stopped.")
            return True
            
        except Exception as e:
            log.error(f"Error stopping Modbus slave '{self.name}': {e}", exc_info=True)
            return False
    
    def _update_modbus_datastore(self, data_type: str, address: int, value: Any):
        """Syncs a single value from our internal dict to the pymodbus datastore."""
        if not self._modbus_context or not self.is_running:
            return
        
        slave_context = self._modbus_context[self.unit_id]
        try:
            log.debug(f"Syncing to datastore: {data_type} @ {address} = {value}")
            if data_type == 'coil':
                slave_context.setValues(1, address, [bool(value)])
            elif data_type == 'discrete_input':
                slave_context.setValues(2, address, [bool(value)])
            elif data_type == 'holding_register':
                slave_context.setValues(3, address, [int(value)])
            elif data_type == 'input_register':
                slave_context.setValues(4, address, [int(value)])
        except Exception as e:
            log.error(f"Failed to sync to datastore for slave '{self.name}': {e}")
    
    def _sync_monitoring_loop(self):
        """
        Periodically checks the pymodbus datastore for changes made by external
        clients and updates our internal dictionaries to reflect them.
        """
        log.info(f"Sync monitoring started for slave '{self.name}'.")
        while self._sync_active and self.is_running:
            try:
                if self._modbus_context:
                    slave_context = self._modbus_context[self.unit_id]
                    # Check Holding Registers
                    for addr, val in self.holding_registers.items():
                        new_val = slave_context.getValues(3, addr, 1)[0]
                        if new_val != val:
                            log.info(f"Detected external write on slave '{self.name}': HR {addr} changed from {val} to {new_val}")
                            self.holding_registers[addr] = new_val
                    # Check Coils
                    for addr, val in self.coils.items():
                        new_val = slave_context.getValues(1, addr, 1)[0]
                        if new_val != val:
                            log.info(f"Detected external write on slave '{self.name}': Coil {addr} changed from {val} to {new_val}")
                            self.coils[addr] = new_val
                
                time.sleep(0.5)  # Check for changes twice per second
            except Exception as e:
                log.error(f"Error in sync monitoring loop for '{self.name}': {e}", exc_info=True)
                time.sleep(2.0) # Wait longer on error
        log.info(f"Sync monitoring stopped for slave '{self.name}'.")

    # --- Public Methods for Data Manipulation ---

    def add_coil(self, address: int, value: bool = False):
        self.coils[address] = value
        self._update_modbus_datastore('coil', address, value)
    
    def add_discrete_input(self, address: int, value: bool = False):
        self.discrete_inputs[address] = value
        self._update_modbus_datastore('discrete_input', address, value)

    def add_holding_register(self, address: int, value: int = 0):
        value = max(0, min(65535, value)) # Ensure 16-bit
        self.holding_registers[address] = value
        self._update_modbus_datastore('holding_register', address, value)

    def add_input_register(self, address: int, value: int = 0):
        value = max(0, min(65535, value)) # Ensure 16-bit
        self.input_registers[address] = value
        self._update_modbus_datastore('input_register', address, value)

    def get_status(self) -> Dict[str, Any]:
        """Returns a dictionary with the slave's current configuration and status."""
        return {
            'name': self.name,
            'unit_id': self.unit_id,
            'port': self.port,
            'is_running': self.is_running,
        }

    def get_all_data(self) -> Dict[str, Dict[int, Any]]:
        """Returns all current data points from internal storage."""
        return {
            'coils': self.coils.copy(),
            'discrete_inputs': self.discrete_inputs.copy(),
            'holding_registers': self.holding_registers.copy(),
            'input_registers': self.input_registers.copy()
        }