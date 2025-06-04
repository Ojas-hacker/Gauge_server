from flask import Flask, request, jsonify, render_template, send_file
from flask_socketio import SocketIO
import os
import csv
import threading
from datetime import datetime

app = Flask(__name__)
app.config['SECRET_KEY'] = 'your-secret-key'
socketio = SocketIO(app)

UPLOAD_FOLDER = 'uploads'
MASTER_CSV = 'master_gauge_readings.csv'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

csv_lock = threading.Lock()

def read_csv_data(date_filter=None):
    data = []
    try:
        if os.path.exists(MASTER_CSV):
            with open(MASTER_CSV, 'r', newline='') as infile:
                reader = csv.DictReader(infile)
                for row in reader:
                    if date_filter:
                        # Extract date from 'time' field, assuming format like 'YYYY-MM-DD HH:MM:SS'
                        row_date = row['time'].split(' ')[0]
                        if row_date == date_filter:
                            data.append(row)
                    else:
                        data.append(row)
    except Exception as e:
        print(f"Error reading CSV: {str(e)}")
    return data

@app.route('/')
def dashboard():
    data = read_csv_data()
    return render_template('index.html', data=data)

@app.route('/data')
def get_data_by_date():
    date = request.args.get('date')
    if not date:
        return jsonify({'error': 'Date parameter is required'}), 400
    try:
        # Validate date format (YYYY-MM-DD)
        datetime.strptime(date, '%Y-%m-%d')
        data = read_csv_data(date_filter=date)
        return jsonify(data)
    except ValueError:
        return jsonify({'error': 'Invalid date format. Use YYYY-MM-DD'}), 400
    except Exception as e:
        return jsonify({'error': f'Failed to fetch data: {str(e)}'}), 500

@app.route('/download_csv')
def download_csv():
    if os.path.exists(MASTER_CSV):
        return send_file(MASTER_CSV, as_attachment=True)
    return jsonify({'error': 'Master CSV not found'}), 404

@app.route('/upload_csv', methods=['POST'])
def upload_csv():
    try:
        if 'file' not in request.files:
            return jsonify({'error': 'No file part in request'}), 400
        file = request.files['file']
        if file.filename == '':
            return jsonify({'error': 'No file selected'}), 400
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f"gauge_readings_{timestamp}.csv"
        file_path = os.path.join(UPLOAD_FOLDER, filename)
        file.save(file_path)
        append_to_master_csv(file_path)
        socketio.emit('csv_update', read_csv_data())
        return jsonify({'message': f'File {filename} uploaded and processed successfully'}), 200
    except Exception as e:
        return jsonify({'error': f'Failed to process file: {str(e)}'}), 500

def append_to_master_csv(file_path):
    try:
        with csv_lock:
            with open(file_path, 'r', newline='') as infile:
                reader = csv.DictReader(infile)
                rows = list(reader)
                if not rows:
                    return
                latest_row = rows[-1]
                fieldnames = ['time', 'gauge-1', 'gauge-2', 'gauge-3', 'gauge-4', 'gauge-5', 'gauge-6', 'gauge-7']
                file_exists = os.path.exists(MASTER_CSV)
                with open(MASTER_CSV, 'a', newline='') as outfile:
                    writer = csv.DictWriter(outfile, fieldnames=fieldnames)
                    if not file_exists:
                        writer.writeheader()
                    writer.writerow(latest_row)
    except Exception as e:
        print(f"Error appending to master CSV: {str(e)}")

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=5000, debug=True)