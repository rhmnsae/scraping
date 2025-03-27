from flask import Flask, render_template, request, jsonify, send_file, session
import os
import pandas as pd
import time
import subprocess
import logging
import random
import json
import threading
import signal
import psutil
import sys
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = 'scraping_x_secret_key'

# Konfigurasi logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
    ]
)

# Variabel global untuk menyimpan status scraping
scraping_status = {
    'is_running': False,
    'progress': 0,
    'messages': [],
    'results': {
        'total': 0,
        'success_count': 0,
        'failed_count': 0,
        'results': []
    },
    'stop_requested': False,
    'current_process': None,  # Untuk menyimpan proses yang sedang berjalan
    'process_pid': None,      # Menyimpan PID sebagai angka yang bisa di-serial
    'keywords': []            # Menyimpan daftar keyword yang sedang/sudah diproses
}

# Thread lock untuk akses status scraping
status_lock = threading.Lock()

# Variabel untuk thread scraping
scraping_thread = None

# Mengambil Data dengan manajemen token otentikasi
def crawl_data(filename, search_keyword, limit, tokens_list):
    global scraping_status
    success = False
    
    # Tambahkan pesan ke log
    add_message_to_log(f"Memulai crawl data untuk: {search_keyword}")
    
    # Coba semua token sampai berhasil atau semua token gagal
    random.shuffle(tokens_list)  # Mengacak urutan token untuk mengurangi kemungkinan token yang sama selalu digunakan pertama
    
    for auth_token in tokens_list:
        # Cek apakah ada permintaan stop
        if get_stop_requested():
            add_message_to_log("Proses crawling dihentikan oleh pengguna.")
            # Periksa apakah file CSV sudah dibuat sebelum berhenti
            check_and_update_csv_files()
            return False
            
        # Gunakan format perintah yang sama persis dengan program Python asli
        crawl_command = (
            f"npx -y tweet-harvest@2.6.1 -o \"{filename}\" -s \"{search_keyword}\" "
            f"--tab \"LATEST\" -l {limit} --token {auth_token}"
        )

        add_message_to_log(f"Mencoba crawl dengan token: {auth_token[:5]}...")
        
        # Menjalankan perintah dengan subprocess.Popen untuk output real-time
        process = subprocess.Popen(
            crawl_command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        
        # Simpan proses PID, bukan objek process-nya
        with status_lock:
            scraping_status['current_process'] = process
            scraping_status['process_pid'] = process.pid if process else None
        
        # Variabel untuk mendeteksi jika token suspended
        token_suspended = False
        
        # Proses output sama persis dengan program Python asli
        for line in process.stdout:
            # Cek apakah ada permintaan stop dan jika file sudah dibuat
            if get_stop_requested():
                # Hentikan proses saat ini
                try:
                    process.terminate()
                    add_message_to_log("Proses crawling dihentikan paksa oleh pengguna.")
                except:
                    pass
                # Periksa file yang sudah dibuat sebelum return
                check_and_update_csv_files()
                return False
                
            line_text = line.decode('utf-8', errors='replace').strip()
            if line_text:
                add_message_to_log(line_text)
            # Deteksi jika token tersuspend atau error lainnya
            if "suspended" in line_text.lower() or "rate limit" in line_text.lower() or "unauthorized" in line_text.lower():
                token_suspended = True
        
        for line in process.stderr:
            # Cek lagi apakah ada permintaan stop
            if get_stop_requested():
                try:
                    process.terminate()
                    add_message_to_log("Proses crawling dihentikan paksa oleh pengguna.")
                except:
                    pass
                # Periksa file yang sudah dibuat sebelum return
                check_and_update_csv_files()
                return False
                
            line_text = line.decode('utf-8', errors='replace').strip()
            if line_text:
                add_message_to_log("ERROR: " + line_text)
            # Deteksi jika token tersuspend atau error lainnya
            if "suspended" in line_text.lower() or "rate limit" in line_text.lower() or "unauthorized" in line_text.lower():
                token_suspended = True
        
        try:
            process.wait(timeout=10)  # Tunggu proses selesai dengan timeout
        except subprocess.TimeoutExpired:
            if get_stop_requested():
                try:
                    process.terminate()
                    add_message_to_log("Proses crawling dihentikan paksa oleh pengguna karena timeout.")
                except:
                    pass
                # Periksa file yang sudah dibuat sebelum return
                check_and_update_csv_files()
                return False
        
        # Reset current process
        with status_lock:
            scraping_status['current_process'] = None
            scraping_status['process_pid'] = None
        
        # Selalu periksa apakah file CSV telah dibuat, terlepas dari status keberhasilan
        if os.path.exists(filename) and os.path.getsize(filename) > 0:
            # Jika file ada dan memiliki data, anggap berhasil meskipun token mungkin terkena suspend
            add_message_to_log(f"Crawl berhasil dengan token: {auth_token[:5]}...")
            success = True
            
            # Update status result untuk file yang ada
            update_results_for_file(search_keyword, filename)
            
            break
        else:
            # Hanya jika file tidak ada atau kosong, coba token berikutnya
            add_message_to_log(f"Token {auth_token[:5]}... gagal atau terkena suspend. Mencoba token berikutnya...")
    
    return success

# Helper functions for thread-safe status updates
def add_message_to_log(message):
    logging.info(message)
    with status_lock:
        scraping_status['messages'].append(message)

def get_stop_requested():
    with status_lock:
        return scraping_status['stop_requested']

def set_progress(progress):
    with status_lock:
        scraping_status['progress'] = progress

def set_running(is_running):
    with status_lock:
        scraping_status['is_running'] = is_running

def set_stop_requested(requested):
    with status_lock:
        scraping_status['stop_requested'] = requested

def update_results_for_file(keyword, filename):
    """Perbarui status results dengan file yang ditemukan"""
    try:
        # Cek jika file ada dan memiliki ukuran
        if os.path.exists(filename) and os.path.getsize(filename) > 0:
            tweet_count = 0
            try:
                # Coba hitung jumlah tweet dalam file
                df = pd.read_csv(filename)
                tweet_count = len(df)
            except:
                pass
            
            with status_lock:
                # Periksa apakah keyword sudah ada dalam hasil
                existing = False
                for i, result in enumerate(scraping_status['results']['results']):
                    if result.get('keyword') == keyword:
                        # Update existing entry
                        scraping_status['results']['results'][i] = {
                            'keyword': keyword,
                            'filename': filename,
                            'count': tweet_count,
                            'status': 'success'
                        }
                        existing = True
                        break
                
                # Jika belum ada, tambahkan baru
                if not existing:
                    scraping_status['results']['results'].append({
                        'keyword': keyword,
                        'filename': filename,
                        'count': tweet_count,
                        'status': 'success'
                    })
                
                # Update status dan hitung
                found_success = sum(1 for r in scraping_status['results']['results'] if r.get('status') == 'success')
                scraping_status['results']['success_count'] = found_success
            
            return True
    except Exception as e:
        logging.error(f"Error updating results for file {filename}: {str(e)}")
    
    return False

def check_and_update_csv_files():
    """Periksa semua file CSV untuk keyword yang diproses dan perbarui status"""
    with status_lock:
        keywords = scraping_status.get('keywords', [])
    
    if not keywords:
        return
    
    found_files = 0
    
    for keyword in keywords:
        filename = f"{keyword.replace(' ', '_')}.csv"
        if update_results_for_file(keyword, filename):
            found_files += 1
    
    if found_files > 0:
        add_message_to_log(f"Ditemukan {found_files} file CSV yang sudah dibuat.")

# Memproses data yang diambil
def process_data(file_path):
    try:
        df = pd.read_csv(file_path)
        add_message_to_log(f"Data berhasil dibaca dari file: {file_path}")
        add_message_to_log(f"Jumlah tweet dalam dataframe: {len(df)}")
        
        # Preview data 
        try:
            preview_data = df.head().to_string()
            add_message_to_log(f"Preview data:\n{preview_data}")
        except Exception as preview_error:
            add_message_to_log(f"Tidak dapat menampilkan preview: {str(preview_error)}")
        
        return True, len(df)
    except FileNotFoundError:
        add_message_to_log(f"Error: File '{file_path}' tidak ditemukan. Pastikan proses crawling berhasil.")
        return False, 0
    except Exception as e:
        add_message_to_log(f"Error saat memproses file: {str(e)}")
        return False, 0

# Memproses beberapa kata kunci
def process_multiple_keywords(keywords, limit, auth_tokens):
    try:
        # Reset status
        with status_lock:
            scraping_status['is_running'] = True
            scraping_status['progress'] = 0
            scraping_status['messages'] = []
            scraping_status['stop_requested'] = False
            scraping_status['current_process'] = None
            scraping_status['process_pid'] = None
            scraping_status['keywords'] = keywords
            scraping_status['results'] = {
                'total': len(keywords),
                'success_count': 0,
                'failed_count': 0,
                'results': []
            }
        
        for index, keyword in enumerate(keywords):
            # Cek apakah ada permintaan stop
            if get_stop_requested():
                add_message_to_log("Proses scraping dihentikan oleh pengguna.")
                # Periksa semua file yang mungkin sudah dibuat
                check_and_update_csv_files()
                break
                
            # Gunakan format filename yang sama persis dengan program Python asli
            filename = f"{keyword.replace(' ', '_')}.csv"
            
            # Periksa apakah file sudah ada, jika ada maka langsung anggap berhasil
            if os.path.exists(filename) and os.path.getsize(filename) > 0:
                add_message_to_log(f"File CSV untuk '{keyword}' sudah ada. Menggunakan file yang ada.")
                
                try:
                    # Baca file yang sudah ada
                    process_success, tweet_count = process_data(filename)
                    if process_success:
                        add_message_to_log(f"Berhasil memproses file yang sudah ada untuk: {keyword}\n")
                        
                        # Update hasil
                        with status_lock:
                            scraping_status['results']['success_count'] += 1
                            scraping_status['results']['results'].append({
                                'keyword': keyword,
                                'filename': filename,
                                'count': tweet_count,
                                'status': 'success'
                            })
                        
                        # Update progress untuk UI
                        set_progress(int(((index + 1) / len(keywords)) * 100))
                        continue  # Lanjut ke keyword berikutnya
                    
                except Exception as e:
                    add_message_to_log(f"Error saat memproses file yang sudah ada: {str(e)}")
                    # Lanjutkan proses crawling normal jika gagal membaca file yang ada
            
            try:
                add_message_to_log(f"Memproses kata kunci: {keyword}")
                
                # Gunakan fungsi crawl_data yang sama dengan program Python asli
                success = crawl_data(filename, keyword, limit, auth_tokens)
                
                # Cek jika proses dihentikan
                if get_stop_requested():
                    add_message_to_log("Proses scraping dihentikan oleh pengguna.")
                    # Periksa semua file yang mungkin sudah dibuat
                    check_and_update_csv_files()
                    break
                    
                # Cek lagi apakah file ada, meskipun success=False
                # Ini penting karena kadang tweet-harvest membuat file tetapi mengembalikan error
                file_exists = os.path.exists(filename) and os.path.getsize(filename) > 0
                
                if success or file_exists:
                    # Coba proses file jika ada
                    if file_exists:
                        process_success, tweet_count = process_data(filename)
                        if process_success:
                            add_message_to_log(f"Selesai memproses kata kunci: {keyword}\n")
                            
                            with status_lock:
                                scraping_status['results']['success_count'] += 1
                                scraping_status['results']['results'].append({
                                    'keyword': keyword,
                                    'filename': filename,
                                    'count': tweet_count,
                                    'status': 'success'
                                })
                        else:
                            add_message_to_log(f"File CSV berhasil dibuat tetapi gagal diproses untuk kata kunci: {keyword}\n")
                            
                            # File ada tapi gagal diproses, tetap dianggap success untuk download
                            with status_lock:
                                scraping_status['results']['success_count'] += 1
                                scraping_status['results']['results'].append({
                                    'keyword': keyword,
                                    'filename': filename,
                                    'count': 0,
                                    'status': 'success'  # Tetap success untuk download
                                })
                    else:
                        add_message_to_log(f"Crawling berhasil tetapi file tidak ditemukan untuk kata kunci: {keyword}\n")
                        
                        with status_lock:
                            scraping_status['results']['failed_count'] += 1
                            scraping_status['results']['results'].append({
                                'keyword': keyword,
                                'filename': None,
                                'count': 0,
                                'status': 'error'
                            })
                else:
                    add_message_to_log(f"Semua token gagal untuk kata kunci: {keyword}. Melewati ke kata kunci berikutnya.\n")
                    
                    with status_lock:
                        scraping_status['results']['failed_count'] += 1
                        scraping_status['results']['results'].append({
                            'keyword': keyword,
                            'filename': None,
                            'count': 0,
                            'status': 'failed'
                        })
                
                # Pause singkat antara keywords - sama dengan program Python asli
                if not get_stop_requested():
                    time.sleep(5)
                
                # Update progress untuk UI
                set_progress(int(((index + 1) / len(keywords)) * 100))
                
            except Exception as e:
                add_message_to_log(f"Error pada keyword {keyword}: {str(e)}")
                
                # Cek jika file ada meskipun terjadi error
                if os.path.exists(filename) and os.path.getsize(filename) > 0:
                    add_message_to_log(f"File CSV berhasil dibuat meskipun terjadi error untuk kata kunci: {keyword}")
                    
                    with status_lock:
                        scraping_status['results']['success_count'] += 1
                        scraping_status['results']['results'].append({
                            'keyword': keyword,
                            'filename': filename,
                            'count': 0,
                            'status': 'success'  # Tetap success untuk download
                        })
                else:
                    with status_lock:
                        scraping_status['results']['failed_count'] += 1
                        scraping_status['results']['results'].append({
                            'keyword': keyword,
                            'filename': None,
                            'count': 0,
                            'status': 'error'
                        })
                
                # Update progress
                set_progress(int(((index + 1) / len(keywords)) * 100))
        
        # Sebelum selesai, periksa lagi semua file CSV untuk memastikan status terkini
        check_and_update_csv_files()
        
        # Update final status
        with status_lock:
            scraping_status['is_running'] = False
            scraping_status['progress'] = 100
            scraping_status['stop_requested'] = False
            scraping_status['current_process'] = None
            scraping_status['process_pid'] = None
        
        # Hitung file yang benar-benar ada untuk laporan akhir
        existing_files = 0
        for result in scraping_status['results']['results']:
            if result['filename'] and os.path.exists(result['filename']):
                existing_files += 1
        
        if get_stop_requested():
            add_message_to_log(f"Scraping dihentikan. {existing_files} dari {len(keywords)} keywords berhasil di-scrape.")
        else:
            add_message_to_log(f"Scraping selesai. {existing_files} dari {len(keywords)} keywords berhasil di-scrape.")
    
    except Exception as e:
        add_message_to_log(f"Error fatal saat memproses keywords: {str(e)}")
        # Pastikan status di-reset meskipun error
        with status_lock:
            scraping_status['is_running'] = False
            scraping_status['progress'] = 100
            scraping_status['stop_requested'] = False
            scraping_status['current_process'] = None
            scraping_status['process_pid'] = None
        # Periksa file CSV yang mungkin sudah dibuat sebelum error
        check_and_update_csv_files()

# Fungsi untuk menghentikan semua proses NPX yang sedang berjalan
def kill_all_npx_processes():
    try:
        # Untuk Windows
        if os.name == 'nt':
            os.system('taskkill /f /im npx.cmd >nul 2>&1')
            # Hati-hati dengan node.exe, bisa menghentikan proses web juga
            # Hanya menghentikan node.exe yang menjalankan tweet-harvest
            for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
                try:
                    if proc.info['name'] == 'node.exe' and proc.info['cmdline']:
                        cmdline = ' '.join(proc.info['cmdline']).lower()
                        if 'tweet-harvest' in cmdline:
                            proc.kill()
                except:
                    continue
        # Untuk Linux/Mac
        else:
            os.system('pkill -f npx')
            os.system('pkill -f tweet-harvest')
        
        add_message_to_log("Semua proses NPX dan Node terkait tweet-harvest dihentikan paksa.")
        return True
    except Exception as e:
        add_message_to_log(f"Error saat menghentikan proses: {str(e)}")
        return False

# Fungsi untuk menghentikan proses scraping saat ini secara paksa
def force_stop_current_process():
    global scraping_status, scraping_thread
    
    # Tandai sebagai diminta berhenti
    set_stop_requested(True)
    
    # Coba menghentikan proses saat ini jika ada
    with status_lock:
        current_process = scraping_status['current_process']
    
    if current_process:
        try:
            current_process.terminate()
            add_message_to_log("Proses crawling saat ini dihentikan.")
        except:
            pass
    
    # Hentikan semua proses npx yang mungkin masih berjalan
    kill_all_npx_processes()
    
    # Periksa file CSV yang mungkin sudah dibuat sebelum dihentikan
    check_and_update_csv_files()
    
    # Tandai sebagai tidak berjalan
    set_running(False)
    
    # Reset thread jika sudah tidak berjalan
    if scraping_thread and not scraping_thread.is_alive():
        scraping_thread = None
    
    return True

# Routes
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/save_tokens', methods=['POST'])
def save_tokens():
    try:
        data = request.json
        tokens = data.get('tokens', [])
        
        # Simpan tokens ke session
        session['tokens'] = tokens
        
        # Simpan tokens ke file untuk penggunaan di masa depan
        with open('tokens.json', 'w') as f:
            json.dump({'tokens': tokens}, f)
        
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

@app.route('/get_tokens')
def get_tokens():
    try:
        # Coba ambil dari session dulu
        tokens = session.get('tokens', [])
        
        # Jika tidak ada di session, coba ambil dari file
        if not tokens and os.path.exists('tokens.json'):
            with open('tokens.json', 'r') as f:
                data = json.load(f)
                tokens = data.get('tokens', [])
                
                # Update session
                session['tokens'] = tokens
        
        return jsonify({'success': True, 'tokens': tokens})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e), 'tokens': []})

@app.route('/start_scraping', methods=['POST'])
def start_scraping():
    global scraping_status, scraping_thread
    
    try:
        # Jika sudah ada proses scraping yang berjalan
        with status_lock:
            is_running = scraping_status['is_running']
        
        if is_running:
            return jsonify({'success': False, 'message': 'Proses scraping sudah berjalan'})
        
        data = request.json
        keywords = data.get('keywords', [])
        limit = data.get('limit', 1000)
        tokens = data.get('tokens', [])
        
        # Validasi input
        if not keywords:
            return jsonify({'success': False, 'message': 'Tidak ada keyword yang disediakan'})
        
        if not tokens:
            return jsonify({'success': False, 'message': 'Tidak ada token yang disediakan'})
        
        # Reset status
        set_stop_requested(False)
        
        # Jalankan proses scraping di thread terpisah
        scraping_thread = threading.Thread(target=process_multiple_keywords, args=(keywords, limit, tokens))
        scraping_thread.daemon = True
        scraping_thread.start()
        
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

@app.route('/scraping_status')
def get_scraping_status():
    # Create a new dictionary with only JSON-serializable data
    with status_lock:
        # Create a shallow copy of status
        safe_status = {
            'is_running': scraping_status.get('is_running', False),
            'progress': scraping_status.get('progress', 0),
            'messages': scraping_status.get('messages', [])[-100:] if scraping_status.get('messages') else [],  # Only last 100 messages
            'stop_requested': scraping_status.get('stop_requested', False),
            'process_pid': scraping_status.get('process_pid'),
            'results': scraping_status.get('results')
        }
    
    try:
        return jsonify(safe_status)
    except Exception as e:
        # If there are still serialization issues, return a minimal response
        logging.error(f"Error serializing status: {str(e)}")
        minimal_status = {
            'is_running': scraping_status.get('is_running', False),
            'progress': scraping_status.get('progress', 0),
            'error': "Error serializing full status"
        }
        return jsonify(minimal_status)

@app.route('/download_csv/<path:filename>')
def download_csv(filename):
    try:
        # Normalisasi nama file
        base_filename = os.path.basename(filename)
        secure_filename_base = secure_filename(base_filename)
        
        # Cek jika filename merupakan jalur lengkap dengan 'tweets-data/'
        if filename.startswith('tweets-data/') or 'tweets-data' in filename:
            # Ambil bagian nama file saja
            file_parts = filename.split('/')
            if len(file_parts) > 1:
                base_filename = file_parts[-1]
                secure_filename_base = secure_filename(base_filename)
            
            # Bangun jalur lengkap ke file di folder tweets-data
            file_path = os.path.join('tweets-data', secure_filename_base)
            
            # Periksa keberadaan file
            if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
                return send_file(file_path, as_attachment=True)
        
        # Cek di folder tweets-data (sebagai fallback)
        tweets_data_path = os.path.join('tweets-data', secure_filename_base)
        if os.path.exists(tweets_data_path) and os.path.getsize(tweets_data_path) > 0:
            return send_file(tweets_data_path, as_attachment=True)
        
        # Cek di folder utama
        regular_path = secure_filename_base
        if os.path.exists(regular_path) and os.path.getsize(regular_path) > 0:
            return send_file(regular_path, as_attachment=True)
        
        # Jika file tidak ditemukan
        return jsonify({'success': False, 'message': f'File {filename} tidak ditemukan'})
    except Exception as e:
        logging.error(f"Error downloading CSV file {filename}: {str(e)}")
        return jsonify({'success': False, 'message': str(e)})

@app.route('/check_file_exists/<path:filename>')
def check_file_exists(filename):
    try:
        # Hilangkan path dari filename jika ada
        base_filename = os.path.basename(filename)
        secure_base_filename = secure_filename(base_filename)
        
        # Periksa di folder utama
        regular_path = secure_base_filename
        regular_exists = os.path.exists(regular_path) and os.path.getsize(regular_path) > 0
        
        # Periksa di folder tweets-data
        tweets_data_path = os.path.join('tweets-data', secure_base_filename)
        tweets_data_exists = os.path.exists(tweets_data_path) and os.path.getsize(tweets_data_path) > 0
        
        # File dinyatakan ada jika ada di salah satu lokasi
        exists = regular_exists or tweets_data_exists
        
        # Beri tahu lokasi file jika ditemukan
        location = None
        if tweets_data_exists:
            location = 'tweets-data'
        elif regular_exists:
            location = 'root'
        
        return jsonify({
            'exists': exists, 
            'location': location
        })
    except Exception as e:
        return jsonify({'exists': False, 'error': str(e)})

@app.route('/stop_scraping', methods=['POST'])
def stop_scraping():
    with status_lock:
        is_running = scraping_status['is_running']
    
    if is_running:
        # Tandai permintaan berhenti
        set_stop_requested(True)
        # Langsung periksa file CSV yang mungkin sudah dibuat
        check_and_update_csv_files()
        return jsonify({
            'success': True, 
            'message': 'Permintaan berhenti sedang diproses. File CSV yang sudah dibuat tetap akan tersedia untuk didownload.'
        })
    else:
        return jsonify({'success': False, 'message': 'Tidak ada proses scraping yang sedang berjalan.'})

@app.route('/force_stop_scraping', methods=['POST'])
def force_stop_scraping():
    with status_lock:
        is_running = scraping_status['is_running']
    
    # Tandai permintaan berhenti
    set_stop_requested(True)
    
    if is_running:
        # Hentikan proses scraping secara paksa
        success = force_stop_current_process()
        
        if success:
            # Periksa semua file CSV yang mungkin sudah dibuat
            check_and_update_csv_files()
            return jsonify({
                'success': True, 
                'message': 'Proses scraping dihentikan paksa. File CSV yang sudah dibuat tetap tersedia untuk didownload.'
            })
        else:
            return jsonify({'success': False, 'message': 'Gagal menghentikan proses secara paksa. Coba refresh halaman.'})
    else:
        # Tetap jalankan kill process meskipun tidak ada proses yang terdeteksi berjalan
        kill_all_npx_processes()
        # Periksa file CSV yang mungkin ada
        check_and_update_csv_files() 
        return jsonify({
            'success': True, 
            'message': 'Semua proses dihentikan paksa. File CSV yang tersedia sudah diperbarui.'
        })
        
        
@app.route('/scan_tweets_data', methods=['POST'])
def scan_tweets_data():
    """Endpoint untuk memindai folder tweets-data dan mengembalikan daftar file CSV"""
    try:
        results = []
        tweets_data_folder = 'tweets-data'
        
        # Pastikan folder tweets-data ada
        if not os.path.exists(tweets_data_folder):
            os.makedirs(tweets_data_folder)
        
        # Mendapatkan semua file CSV dari folder tweets-data
        csv_files = [f for f in os.listdir(tweets_data_folder) if f.lower().endswith('.csv')]
        found_files = len(csv_files)
        
        # Proses setiap file CSV
        for filename in csv_files:
            file_path = os.path.join(tweets_data_folder, filename)
            tweet_count = 0
            
            try:
                # Hitung jumlah tweet dalam file
                df = pd.read_csv(file_path)
                tweet_count = len(df)
                
                # Coba mengekstrak keyword dari nama file
                keyword = filename.replace('.csv', '').replace('_', ' ')
                
                results.append({
                    'keyword': keyword,
                    'filename': os.path.join(tweets_data_folder, filename),  # Gunakan path lengkap
                    'count': tweet_count,
                    'status': 'success',
                    'is_tweets_data': True  # Tandai file dari folder tweets-data
                })
            except Exception as e:
                logging.error(f"Error membaca file {file_path}: {str(e)}")
                results.append({
                    'keyword': filename,
                    'filename': os.path.join(tweets_data_folder, filename),  # Gunakan path lengkap
                    'count': 0,
                    'status': 'error',
                    'is_tweets_data': True
                })
        
        # Update status results dengan file dari tweets-data
        with status_lock:
            # Inisialisasi results jika belum ada
            if 'results' not in scraping_status:
                scraping_status['results'] = {
                    'total': 0,
                    'success_count': 0,
                    'failed_count': 0,
                    'results': []
                }
            
            # Hapus entri yang memiliki is_tweets_data=True (untuk menghindari duplikat)
            scraping_status['results']['results'] = [
                r for r in scraping_status['results']['results'] 
                if not r.get('is_tweets_data', False)
            ]
            
            # Tambahkan hasil terbaru dari tweets-data
            scraping_status['results']['results'].extend(results)
            
            # Update jumlah file berhasil
            scraping_status['results']['success_count'] = sum(
                1 for r in scraping_status['results']['results'] 
                if r.get('status') == 'success'
            )
            
            scraping_status['results']['total'] = len(scraping_status['results']['results'])
        
        return jsonify({
            'success': True,
            'message': f'Berhasil memindai folder tweets-data, ditemukan {found_files} file CSV',
            'results': scraping_status['results'],
            'found_files': found_files
        })
        
    except Exception as e:
        logging.error(f"Error scanning tweets-data folder: {str(e)}")
        return jsonify({
            'success': False,
            'message': f'Error: {str(e)}',
            'results': scraping_status.get('results', {
                'total': 0,
                'success_count': 0,
                'failed_count': 0,
                'results': []
            })
        })


@app.route('/refresh_csv_files', methods=['POST'])
def refresh_csv_files():
    """Endpoint untuk menyegarkan daftar file CSV yang tersedia"""
    try:
        # Periksa apakah ada request JSON, jika tidak, gunakan dict kosong
        try:
            data = request.get_json(silent=True) or {}
        except:
            data = {}
        
        scan_tweets_data_folder = data.get('scan_tweets_data', False)
        
        # Periksa file CSV di direktori utama
        check_and_update_csv_files()
        
        # Jika diminta, juga pindai folder tweets-data
        if scan_tweets_data_folder:
            return scan_tweets_data()
        
        # Jika tidak, langsung jalankan scan_tweets_data
        # karena kita ingin selalu mengecek folder tweets-data
        scan_result = scan_tweets_data()
        scan_data = scan_result.get_json()
        
        # Lalu kembalikan hasilnya
        with status_lock:
            results = scraping_status.get('results', {
                'total': 0,
                'success_count': 0,
                'failed_count': 0,
                'results': []
            })
        
        return jsonify({
            'success': True,
            'message': 'Daftar file CSV diperbarui',
            'results': results,
            'found_files': scan_data.get('found_files', 0) if scan_data else 0
        })
    except Exception as e:
        logging.error(f"Error refreshing CSV files: {str(e)}")
        return jsonify({
            'success': False,
            'message': f'Error: {str(e)}',
            'results': scraping_status.get('results', {})
        })
        

if __name__ == '__main__':
    # Pastikan tidak ada proses lama yang masih berjalan
    try:
        kill_all_npx_processes()
    except:
        pass
    
    app.run(debug=True)