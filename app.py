import os
import glob
from flask import Flask, render_template_string, request, redirect, url_for, flash
# Importiert die Funktion aus deiner neu erstellten importer.py
from importer import import_payout_files

app = Flask(__name__)
app.secret_key = "geheim_fuer_flash_nachrichten"

# Ordner-Konfiguration
UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

MASTER_CSV_PATH = os.path.join(os.path.dirname(__file__), 'Master_Payouts.csv')

# Einfaches HTML-Template für Webansicht
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="de">
<head>
    <meta charset="UTF-8">
    <title>eBay Payout Import System</title>
    <style>
        body { font-family: Arial, sans-serif; margin: 40px; line-height: 1.6; background-color: #f4f4f9; }
        .container { max-width: 700px; background: white; padding: 25px; border-radius: 8px; box-shadow: 0 0 10px rgba(0,0,0,0.1); }
        h1 { color: #333; }
        .upload-btn { background-color: #0066cc; color: white; padding: 10px 15px; border: none; border-radius: 4px; cursor: pointer; }
        .upload-btn:hover { background-color: #004b99; }
        .alert { padding: 15px; background-color: #d4edda; color: #155724; border-radius: 4px; margin-bottom: 20px; }
    </style>
</head>
<body>
    <div class="container">
        <h1>eBay Payout Importer</h1>
        
        {% with messages = get_flashed_messages() %}
          {% if messages %}
            {% for message in messages %}
              <div class="alert">{{ message }}</div>
            {% endfor %}
          {% endif %}
        {% endwith %}

        <form method="post" action="/upload" enctype="multipart/form-data">
            <p>Wähle eine oder mehrere eBay-Payout CSV-Dateien aus:</p>
            <input type="file" name="file" multiple required>
            <br><br>
            <button type="submit" class="upload-btn">Dateien hochladen & verarbeiten</button>
        </form>
    </div>
</body>
</html>
"""

@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route('/upload', methods=['POST'])
def upload_file():
    if 'file' not in request.files:
        flash('Keine Datei ausgewählt.')
        return redirect(url_for('index'))
    
    files = request.files.getlist('file')
    
    for file in files:
        if file.filename != '':
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], file.filename)
            file.save(filepath)
    
    # Automatische Verarbeitung & Dublettenprüfung starten
    import_payout_files(
        input_directory=app.config['UPLOAD_FOLDER'], 
        output_master_csv=MASTER_CSV_PATH
    )
    
    flash('Dateien erfolgreich verarbeitet! Dubletten wurden automatisch gefiltert.')
    return redirect(url_for('index'))

if __name__ == '__main__':
    app.run(debug=True, port=5000)
