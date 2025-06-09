from flask import Flask, request, jsonify, render_template, send_file
from flask_socketio import SocketIO
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import os
import csv
from datetime import datetime
from pymongo import MongoClient
from bson import json_util
import json
import logging
import zipfile
import tempfile
from waitress import serve

app = Flask(__name__)
app.config['SECRET_KEY'] = 'your-secret-key'
socketio = SocketIO(app, async_mode='threading')

UPLOAD_FOLDER = 'Uploads'
TEMP_DOWNLOAD_FOLDER = 'TempDownloads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(TEMP_DOWNLOAD_FOLDER, exist_ok=True)

# Setup server logging
SERVER_LOG_FILE = os.path.join(UPLOAD_FOLDER, 'server.log')
logging.basicConfig(
    filename=SERVER_LOG_FILE,
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# MongoDB setup
client = MongoClient('mongodb://localhost:27017')
db = client['gauge_monitor']
readings_collection = db['readings']
download_tracker_collection = db['download_tracker']

# Scheduler setup
scheduler = BackgroundScheduler()

def delete_all_data():
    try:
        result = readings_collection.delete_many({})
        logger.info(f"MONTHLY_DATA_DELETION, Deleted {result.deleted_count} documents, Status: SUCCESS")
    except Exception as e:
        logger.error(f"MONTHLY_DATA_DELETION, Status: FAILED, Error: {str(e)}")

def auto_download_new_data():
    try:
        # Get latest downloaded timestamp
        latest_download = download_tracker_collection.find_one(
            sort=[('timestamp', -1)]
        )
        last_download_time = latest_download['timestamp'] if latest_download else None

        # Define query for new data
        query = {}
        if last_download_time:
            query['time'] = {'$gt': last_download_time}

        fieldnames = ['time', 'gauge-1', 'gauge-2', 'gauge-3', 'gauge-4', 'gauge-5', 'gauge-6', 'gauge-7']
        total_count = readings_collection.count_documents(query)

        if total_count == 0:
            logger.info("AUTO_DOWNLOAD, No new data to download, Status: SKIPPED")
            return

        # Generate temporary CSV file
        current_date = datetime.now().strftime('%Y-%m-%d')
        filename_prefix = f'auto_gauge_readings_{current_date}'
        output_file = os.path.join(TEMP_DOWNLOAD_FOLDER, f'{filename_prefix}.csv')

        data = list(readings_collection.find(query).sort('time', -1))
        with open(output_file, 'w', newline='') as outfile:
            writer = csv.DictWriter(outfile, fieldnames=fieldnames)
            writer.writeheader()
            for row in data:
                row_data = {k: row.get(k, 'N/A') for k in fieldnames}
                writer.writerow(row_data)

        # Record download in tracker
        download_tracker_collection.insert_one({
            'timestamp': data[0]['time'],
            'file': output_file,
            'record_count': total_count,
            'created_at': datetime.now()
        })

        # Notify clients to download
        download_url = f'/download_auto_csv?file={os.path.basename(output_file)}'
        socketio.emit('auto_download', {'url': download_url})
        logger.info(f"AUTO_DOWNLOAD, Filename: {output_file}, Records: {total_count}, Status: SUCCESS")
    except Exception as e:
        logger.error(f"AUTO_DOWNLOAD, Status: FAILED, Error: {str(e)}")

# Schedule tasks
scheduler.add_job(
    delete_all_data,
    trigger=CronTrigger(day=1, hour=0, minute=0),
    id='monthly_data_deletion',
    replace_existing=True
)
scheduler.add_job(
    auto_download_new_data,
    trigger=CronTrigger(hour=12, minute=0),
    id='daily_auto_download',
    replace_existing=True
)

# Start scheduler
try:
    scheduler.start()
except Exception as e:
    logger.error(f"Failed to start scheduler: {str(e)}")

# Cleanup
import atexit
def cleanup():
    try:
        scheduler.shutdown()
        logger.info("Scheduler shut down successfully")
    except Exception as e:
        logger.error(f"Failed to shut down scheduler: {str(e)}")
    try:
        socketio.stop()
        logger.info("SocketIO shut down successfully")
    except Exception as e:
        logger.error(f"Failed to shut down SocketIO: {str(e)}")
    # Clean temp download folder
    try:
        for file in os.listdir(TEMP_DOWNLOAD_FOLDER):
            os.remove(os.path.join(TEMP_DOWNLOAD_FOLDER, file))
        logger.info("Cleaned temporary download folder")
    except Exception as e:
        logger.error(f"Failed to clean temp download folder: {str(e)}")

atexit.register(cleanup)

def read_data(date_filter=None, limit=10):
    try:
        query = {}
        if date_filter:
            query['time'] = {'$regex': f'^{date_filter}'}
        data = list(readings_collection.find(query).sort('time', -1).limit(limit))
        return json.loads(json_util.dumps(data))
    except Exception as e:
        logger.error(f"Error reading from MongoDB: {str(e)}")
        return []

def read_client_logs():
    try:
        logs = []
        log_file = os.path.join(UPLOAD_FOLDER, 'gauge_reader.log')
        if os.path.exists(log_file):
            with open(log_file, 'r') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        if line.startswith('[') and ']' in line:
                            timestamp_end = line.index(']') + 1
                            timestamp = line[1:timestamp_end-1]
                            message = line[timestamp_end+1:].strip()
                        else:
                            timestamp = 'N/A'
                            message = line
                        
                        log_entry = {'timestamp': timestamp, 'message': message}
                        if 'Gauge' in message and ':' in message:
                            parts = message.split(':', 1)
                            gauge = parts[0].strip()
                            status = parts[1].strip()
                            log_entry.update({
                                'action': f'PROCESS_{gauge.upper().replace(" ", "_")}',
                                'status': 'SUCCESS' if 'Unable to read' not in status else 'FAILED',
                                'error': status if 'Unable to read' in status else 'N/A'
                            })
                        elif 'Error' in message:
                            log_entry.update({
                                'action': 'PROCESS_ERROR',
                                'status': 'FAILED',
                                'error': message
                            })
                        else:
                            log_entry.update({
                                'action': message.split(' ')[0].upper(),
                                'status': 'INFO',
                                'error': 'N/A'
                            })
                        logs.append(log_entry)
                    except:
                        logs.append({
                            'timestamp': timestamp or 'N/A',
                            'message': message,
                            'action': 'UNKNOWN',
                            'status': 'INFO',
                            'error': 'N/A'
                        })
        return logs[-10:]
    except Exception as e:
        logger.error(f"Error reading client logs from gauge_reader.log: {str(e)}")
        return []

@app.route('/')
def dashboard():
    data = read_data()
    logger.info(f"Data sent to template: {data}")
    return render_template('index.html', data=data)

@app.route('/data')
def get_data_by_date():
    date = request.args.get('date')
    if not date:
        return jsonify({'error': 'Date parameter is required'}), 400
    try:
        datetime.strptime(date, '%Y-%m-%d')
        data = read_data(date_filter=date)
        return jsonify(data)
    except ValueError:
        return jsonify({'error': 'Invalid date format. Use YYYY-MM-DD'}), 400
    except Exception as e:
        logger.error(f"Failed to fetch data: {str(e)}")
        return jsonify({'error': f'Failed to fetch data: {str(e)}'}), 500

@app.route('/download_csv')
def download_csv():
    date = request.args.get('date')
    try:
        fieldnames = ['time', 'gauge-1', 'gauge-2', 'gauge-3', 'gauge-4', 'gauge-5', 'gauge-6', 'gauge-7']
        
        if date:
            try:
                datetime.strptime(date, '%Y-%m-%d')
                query = {'time': {'$regex': f'^{date}'}}
                filename_prefix = f'gauge_readings_{date}'
            except ValueError:
                return jsonify({'error': 'Invalid date format. Use YYYY-MM-DD'}), 400
        else:
            query = {}
            filename_prefix = 'master_gauge_readings'
        
        total_count = readings_collection.count_documents(query)
        
        if total_count == 0:
            return jsonify({'error': 'No data found for the specified criteria'}), 404
        
        if total_count <= 1000000:
            data = list(readings_collection.find(query).sort('time', -1))
            output_file = os.path.join(TEMP_DOWNLOAD_FOLDER, f'{filename_prefix}.csv')
            
            with open(output_file, 'w', newline='') as outfile:
                writer = csv.DictWriter(outfile, fieldnames=fieldnames)
                writer.writeheader()
                for row in data:
                    row_data = {k: row.get(k, 'N/A') for k in fieldnames}
                    writer.writerow(row_data)
            
            response = send_file(output_file, as_attachment=True, download_name=f'{filename_prefix}.csv')
            
            import threading
            def delete_file():
                import time
                time.sleep(2)
                try:
                    os.remove(output_file)
                    logger.info(f"Deleted temporary file: {output_file}")
                except OSError as e:
                    logger.error(f"Failed to delete file {output_file}: {str(e)}")
            
            thread = threading.Thread(target=delete_file)
            thread.daemon = True
            thread.start()
            
            return response
        
        else:
            temp_dir = tempfile.mkdtemp()
            zip_filename = f'{filename_prefix}_multiple_files.zip'
            zip_path = os.path.join(temp_dir, zip_filename)
            
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
                skip = 0
                file_count = 1
                
                while skip < total_count:
                    batch_data = list(readings_collection.find(query).sort('time', -1).skip(skip).limit(1000000))
                    
                    if not batch_data:
                        break
                    
                    csv_filename = f'{filename_prefix}_part_{file_count}.csv'
                    csv_path = os.path.join(temp_dir, csv_filename)
                    
                    with open(csv_path, 'w', newline='') as outfile:
                        writer = csv.DictWriter(outfile, fieldnames=fieldnames)
                        writer.writeheader()
                        for row in batch_data:
                            row_data = {k: row.get(k, 'N/A') for k in fieldnames}
                            writer.writerow(row_data)
                    
                    zipf.write(csv_path, csv_filename)
                    os.remove(csv_path)
                    
                    skip += 1000000
                    file_count += 1
            
            response = send_file(zip_path, as_attachment=True, download_name=zip_filename)
            
            import threading
            import shutil
            def cleanup_temp_dir():
                import time
                time.sleep(3)
                try:
                    shutil.rmtree(temp_dir)
                    logger.info(f"Deleted temporary directory: {temp_dir}")
                except OSError as e:
                    logger.error(f"Failed to delete temp directory {temp_dir}: {str(e)}")
            
            thread = threading.Thread(target=cleanup_temp_dir)
            thread.daemon = True
            thread.start()
            
            return response
            
    except Exception as e:
        logger.error(f"Failed to generate CSV: {str(e)}")
        return jsonify({'error': f'Failed to generate CSV: {str(e)}'}), 500

@app.route('/download_auto_csv')
def download_auto_csv():
    try:
        filename = request.args.get('file')
        if not filename:
            return jsonify({'error': 'File parameter is required'}), 400
        file_path = os.path.join(TEMP_DOWNLOAD_FOLDER, filename)
        if not os.path.exists(file_path):
            return jsonify({'error': 'File not found'}), 404
        
        response = send_file(file_path, as_attachment=True, download_name=filename)
        
        import threading
        def delete_file():
            import time
            time.sleep(2)
            try:
                os.remove(file_path)
                logger.info(f"Deleted temporary file: {file_path}")
            except OSError as e:
                logger.error(f"Failed to delete file {file_path}: {str(e)}")
        
        thread = threading.Thread(target=delete_file)
        thread.daemon = True
        thread.start()
        
        return response
    except Exception as e:
        logger.error(f"Failed to download auto CSV: {str(e)}")
        return jsonify({'error': f'Failed to download auto CSV: {str(e)}'}), 500

@app.route('/upload_csv', methods=['POST'])
def upload_csv():
    try:
        if 'file' not in request.files:
            return jsonify({'error': 'No file part in request'}), 400
        file = request.files['file']
        if file.filename == '':
            return jsonify({'error': 'No file selected'}), 400
        
        for old_file in os.listdir(UPLOAD_FOLDER):
            if old_file.endswith('.csv'):
                file_path = os.path.join(UPLOAD_FOLDER, old_file)
                try:
                    os.remove(file_path)
                except:
                    pass
        
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f"gauge_readings_{timestamp}.csv"
        file_path = os.path.join(UPLOAD_FOLDER, filename)
        file.save(file_path)
        process_and_store_to_mongodb(file_path)
        socketio.emit('csv_update', read_data())
        
        logger.info(f"CSV_UPLOAD, Filename: {filename}, Status: SUCCESS")
        return jsonify({'message': f'File {filename} uploaded and processed successfully'}), 200
    except Exception as e:
        logger.error(f"CSV_UPLOAD, Filename: {file.filename if 'file' in locals() else 'UNKNOWN'}, Status: FAILED, Error: {str(e)}")
        return jsonify({'error': f'Failed to process file: {str(e)}'}), 500

def process_and_store_to_mongodb(file_path):
    try:
        with open(file_path, 'r', newline='') as infile:
            reader = csv.DictReader(infile)
            rows = list(reader)
            if not rows:
                return
            latest_row = rows[-1]
            fieldnames = ['time', 'gauge-1', 'gauge-2', 'gauge-3', 'gauge-4', 'gauge-5', 'gauge-6', 'gauge-7']
            row_data = {key: latest_row.get(key, 'N/A') for key in fieldnames}
            logger.info(f"Inserting data into MongoDB: {row_data}")
            readings_collection.insert_one(row_data)
            os.remove(file_path)
            logger.info(f"PROCESS_CSV, Filename: {os.path.basename(file_path)}, Status: SUCCESS")
    except Exception as e:
        logger.error(f"PROCESS_CSV, Filename: {os.path.basename(file_path)}, Status: FAILED, Error: {str(e)}")

if __name__ == '__main__':
    try:
        logger.info("Starting Waitress server on host=0.0.0.0, port=5000")
        serve(app, host='0.0.0.0', port=5000, threads=4)
    except Exception as e:
        logger.error(f"Failed to start Waitress server: {str(e)}")
        cleanup()
