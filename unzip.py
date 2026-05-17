import zipfile
import os
from pathlib import Path

def unzip(zip_path, target_dir):
    #---WYPAKOWANIE BEZPOSREDNIO DO FOLDEROW, TO SIE POZNIEJ WYKLEPIE---
    with zipfile.ZipFile(zip_path, 'r') as zip_ref:
        for member in zip_ref.infolist():
            if member.is_dir():
                continue
            
            filename = os.path.basename(member.filename)
            if not filename:
                continue
                
            target_path = os.path.join(target_dir, filename)
            
            # Używamy podwójnego 'with' dla pewności zamknięcia obu plików
            with zip_ref.open(member) as source:
                with open(target_path, "wb") as target:
                    target.write(source.read())
                
    print(f"Wypakowano {os.path.basename(zip_path)} do {target_dir}.")

#unzip(r'C:\Users\olaa3\Desktop\SKOROWIDZE\ASCII\ASCII_TBD\73727_1018381_6.161.33.03.1.zip')