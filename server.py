from flask import Flask, request, jsonify, render_template, Response, send_file
from pymongo import MongoClient
import csv
from io import StringIO
import datetime
import os
import logging
import json
import time
import schedule
import threading
import pendulum
from flask_cors import CORS

# Configure logging
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')

app = Flask(__name__)
CORS(app)
client = MongoClient('mongodb://localhost:27017/')
db = client['gauge_readings']
collection = db['readings']
downloads_collection = db['downloads']

# Ensure csv_downloads directory exists
CSV_DIR = 'csv_downloads'
if not os.path.exists(CSV_DIR):
    os.makedirs(CSV_DIR)

def cleanup_database():
    """Delete all documents in the readings collection."""
    try:
        collection_count = collection.count_documents({})
        if collection_count > 0:
            collection.drop()
            logging.info(f"Cleaned database: dropped 'readings' collection with {collection_count} documents")
        else:
            logging.info("No documents to clean in 'readings' collection")
    except Exception as e:
        logging.error(f"Error cleaning database: {str(e)}")

def generate_daily_csv():
    """Generate CSV files for new data, split at 100,000 rows, and mark as downloaded."""
    try:
        now = pendulum.now('Asia/Kolkata')
        timestamp_str = now.strftime('%Y%m%d_%H%M')
        
        for panel in [1, 2]:
            # Find undownloaded documents
            downloaded_ids = {doc['document_id'] for doc in downloads_collection.find({'panel': panel})}
            query = {'panel': panel, '_id': {'$nin': list(downloaded_ids)}}
            cursor = collection.find(query).sort('timestamp', 1)
            documents = list(cursor)
            total_docs = len(documents)
            
            if total_docs == 0:
                logging.info(f"No new data to export for panel {panel}")
                continue
            
            # Split into chunks of 100,000
            chunk_size = 100000
            for i in range(0, total_docs, chunk_size):
                chunk = documents[i:i + chunk_size]
                part = (i // chunk_size) + 1
                filename = f"panel{panel}_{timestamp_str}_part{part}.csv"
                filepath = os.path.join(CSV_DIR, filename)
                
                # Write CSV
                with open(filepath, 'w', newline='') as csvfile:
                    if chunk:
                        writer = csv.DictWriter(csvfile, fieldnames=chunk[0].keys())
                        writer.writeheader()
                        for doc in chunk:
                            doc_copy = doc.copy()
                            doc_copy['_id'] = str(doc_copy['_id'])
                            doc_copy['timestamp'] = doc_copy['timestamp'].strftime('%Y-%m-%d %H:%M:%S')
                            writer.writerow(doc_copy)
                
                # Mark documents as downloaded
                downloaded_ids = [doc['_id'] for doc in chunk]
                downloads_collection.insert_many([
                    {'document_id': doc_id, 'panel': panel, 'downloaded_at': now.to_datetime_string()}
                    for doc_id in downloaded_ids
                ])
                
                logging.info(f"Generated CSV {filename} with {len(chunk)} rows for panel {panel}")
        
    except Exception as e:
        logging.error(f"Error generating daily CSV: {str(e)}")

def run_scheduler():
    """Run scheduled tasks in a background thread."""
    # Schedule database cleanup every 28 days
    schedule.every(28).days.at("00:00", tz=pendulum.timezone("Asia/Kolkata")).do(cleanup_database)
    # Schedule CSV generation every day at 1:00 PM IST
    schedule.every(1).days.at("13:00", tz=pendulum.timezone("Asia/Kolkata")).do(generate_daily_csv)
    logging.info("Scheduled tasks: database cleanup every 28 days, CSV generation daily at 1:00 PM IST")
    while True:
        schedule.run_pending()
        time.sleep(60)

@app.route('/upload_csv', methods=['POST'])
def upload_csv():
    try:
        panel = int(request.form.get('panel'))
        csv_file = request.files['file']
        
        content = csv_file.read().decode('utf-8')
        csv_data = StringIO(content)
        reader = csv.DictReader(csv_data)
        
        for row in reader:
            row['panel'] = panel
            row['timestamp'] = datetime.datetime.strptime(row['timestamp'], '%Y-%m-%d %H:%M:%S')
            for key in row:
                if key not in ['timestamp', 'panel']:
                    try:
                        row[key] = float(row[key].split(' ')[0]) if row[key] != 'Error' else None
                    except (ValueError, IndexError):
                        row[key] = None
            collection.insert_one(row)
        
        logging.info(f"Uploaded CSV data for panel {panel}")
        return jsonify({'status': 'success', 'message': f'Uploaded data for panel {panel}'}), 200
    except Exception as e:
        logging.error(f"Error in upload_csv: {str(e)}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/latest_readings', methods=['GET'])
def latest_readings():
    try:
        latest_panel1 = collection.find({'panel': 1}).sort('timestamp', -1).limit(1)
        latest_panel2 = collection.find({'panel': 2}).sort('timestamp', -1).limit(1)
        
        panel1_data = next(latest_panel1, {})
        panel2_data = next(latest_panel2, {})
        
        if panel1_data:
            panel1_data['_id'] = str(panel1_data['_id'])
            panel1_data['timestamp'] = panel1_data['timestamp'].strftime('%Y-%m-%d %H:%M:%S')
        if panel2_data:
            panel2_data['_id'] = str(panel2_data['_id'])
            panel2_data['timestamp'] = panel2_data['timestamp'].strftime('%Y-%m-%d %H:%M:%S')
        
        logging.debug(f"Latest readings: panel1={panel1_data}, panel2={panel2_data}")
        return jsonify({
            'panel1': panel1_data,
            'panel2': panel2_data
        })
    except Exception as e:
        logging.error(f"Error in latest_readings: {str(e)}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/historical_data', methods=['GET'])
def historical_data():
    try:
        panel = request.args.get('panel', type=int)
        hours = request.args.get('hours', default=1, type=int)
        end_time = datetime.datetime.now()
        start_time = end_time - datetime.timedelta(hours=hours)
        
        query = {
            'panel': panel,
            'timestamp': {
                '$gte': start_time,
                '$lte': end_time
            }
        }
        data = collection.find(query).sort('timestamp', 1)
        result = [{
            'timestamp': doc['timestamp'].strftime('%Y-%m-%d %H:%M:%S'),
            **{k: v for k, v in doc.items() if k not in ['_id', 'panel', 'timestamp']}
        } for doc in data]
        
        logging.debug(f"Historical data for panel {panel}: {len(result)} records")
        return jsonify(result)
    except Exception as e:
        logging.error(f"Error in historical_data: {str(e)}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/stream_readings')
def stream_readings():
    def generate():
        with collection.watch([{'$match': {'operationType': 'insert'}}]) as stream:
            for change in stream:
                doc = change['fullDocument']
                doc['_id'] = str(doc['_id'])
                doc['timestamp'] = doc['timestamp'].strftime('%Y-%m-%d %H:%M:%S')
                yield f"data: {json.dumps({'panel' + str(doc['panel']): doc})}\n\n"
                logging.debug(f"Streamed new reading for panel {doc['panel']}")
    
    return Response(generate(), mimetype='text/event-stream')

@app.route('/list_csv_files', methods=['GET'])
def list_csv_files():
    try:
        today = pendulum.now('Asia/Kolkata').strftime('%Y%m%d')
        csv_files = [f for f in os.listdir(CSV_DIR) if f.startswith('panel') and today in f]
        return jsonify({'files': csv_files})
    except Exception as e:
        logging.error(f"Error listing CSV files: {str(e)}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/download_csv/<filename>', methods=['GET'])
def download_csv(filename):
    try:
        filepath = os.path.join(CSV_DIR, filename)
        if os.path.exists(filepath):
            return send_file(filepath, as_attachment=True)
        else:
            return jsonify({'status': 'error', 'message': 'File not found'}), 404
    except Exception as e:
        logging.error(f"Error downloading CSV {filename}: {str(e)}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/')
def dashboard():
    return render_template('dashboard.html')

if __name__ == '__main__':
    scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
    scheduler_thread.start()
    from waitress import serve
    serve(app, host='0.0.0.0', port=5000, threads=6)
