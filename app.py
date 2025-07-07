# app.py (Refactored)

import threading
import logging
from flask import Flask, render_template, request, jsonify

from modbus_server import ModbusServer

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = Flask(__name__)

# --- Global State: Dictionary of servers, keyed by port number ---
MODBUS_SERVERS: dict[int, ModbusServer] = {}
STATE_LOCK = threading.Lock()

# --- API Routes ---

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/slaves', methods=['GET'])
def get_slaves():
    """Returns a flat list of all slave units across all servers."""
    all_slaves = []
    with STATE_LOCK:
        for port, server in MODBUS_SERVERS.items():
            for unit_id in server.slave_data.keys():
                all_slaves.append({
                    'port': port,
                    'unit_id': unit_id,
                    'is_running': server.is_running
                })
    return jsonify(all_slaves)

@app.route('/api/slaves', methods=['POST'])
def create_slave():
    """Creates a new slave unit, starting a new server if needed."""
    data = request.json
    try:
        port = int(data['port'])
        unit_id = int(data['unit_id'])
    except (ValueError, TypeError, KeyError):
        return jsonify({'error': 'Port and Unit ID must be provided as integers'}), 400

    with STATE_LOCK:
        server = MODBUS_SERVERS.get(port)
        if server:
            # Server on this port already exists, just add the unit
            if unit_id in server.slave_data:
                return jsonify({'error': f'Unit ID {unit_id} already exists on port {port}'}), 409
            server.add_slave_unit(unit_id)
        else:
            # No server on this port, create a new one
            server = ModbusServer(port=port)
            server.add_slave_unit(unit_id)
            if not server.start():
                return jsonify({'error': f'Failed to start server on port {port}'}), 500
            MODBUS_SERVERS[port] = server
        
    return jsonify({'message': f'Slave unit {unit_id} on port {port} created successfully'}), 201

@app.route('/api/slaves/<int:port>/<int:unit_id>', methods=['DELETE'])
def delete_slave(port, unit_id):
    """Deletes a slave unit. If it's the last one, the server is stopped."""
    with STATE_LOCK:
        server = MODBUS_SERVERS.get(port)
        if not server or unit_id not in server.slave_data:
            return jsonify({'error': 'Slave unit not found'}), 404

        server.remove_slave_unit(unit_id)
        
        # If no units are left on this server, stop and remove it
        if not server.has_units():
            server.stop()
            del MODBUS_SERVERS[port]
            log.info(f"Stopped and removed server on port {port} as it has no more units.")

    return jsonify({'message': 'Slave unit deleted successfully'}), 200

@app.route('/api/slaves/<int:port>/<int:unit_id>/data', methods=['GET'])
def get_slave_data(port, unit_id):
    """Gets all data points for a specific slave unit."""
    with STATE_LOCK:
        server = MODBUS_SERVERS.get(port)
        if not server:
            return jsonify({'error': 'Server not found'}), 404
        data = server.get_all_data_for_unit(unit_id)
        if data is None:
            return jsonify({'error': 'Slave unit not found'}), 404

    # Sort data by address for a consistent UI
    for key in data:
        data[key] = dict(sorted(data[key].items()))
        
    return jsonify(data)

@app.route('/api/slaves/<int:port>/<int:unit_id>/data', methods=['POST'])
def update_slave_data(port, unit_id):
    """Updates or adds a data point for a specific slave unit."""
    data = request.json
    try:
        data_type = data['type']
        address = int(data['address'])
        value = data['value']
    except (ValueError, TypeError, KeyError):
        return jsonify({'error': 'Invalid request format'}), 400

    with STATE_LOCK:
        server = MODBUS_SERVERS.get(port)
        if not server:
            return jsonify({'error': 'Server not found'}), 404
        
        server.set_data_point(unit_id, data_type, address, value)

    return jsonify({'message': 'Data updated successfully'})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, threaded=True, debug=False)