import io
import json
import os
import sys
from pathlib import Path

import geopandas as gpd
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed


def download_single_file(url_input, target_dir):
    #---POBIERA POJEDYNCZY NMT Z URL---
    try:
        #wyciagam ze slownika tylko url (jak jest)
        if isinstance(url_input, dict):
            url = url_input.get('url')
        else:
            url = url_input
        file_name = url.split('/')[-1]
        file_path = os.path.join(target_dir, file_name)

        if os.path.exists(file_path):
            #jesli plik ma wagę >5 KB, to blad HTML, pobieram od nowa
            if os.path.getsize(file_path) > 5000:
                return f"[DOWNLOADER] Pominieto (istnieje): {file_name}"

        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Referer': 'https://geoportal.gov.pl/'}

        response = requests.get(url, headers=headers, stream=True, timeout=30)
        
        if response.status_code == 200:
            if 'text/html' in response.headers.get('Content-Type', ''):
                return f"[DOWNLOADER] Blad serwera: {file_name}"
                
            with open(file_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
            return f"[DOWNLOADER] Pobrano: {file_name} ({round(os.path.getsize(file_path)/(1024*1024), 2)} MB)"
        else:
            return f"[DOWNLOADER] Blad serwera {response.status_code}: {file_name}"
    except Exception as e:
        return f"[DOWNLOADER] Blad podczas pobierania {url}: {e}"


def download_nmt_files(links, target_dir, max_workers=3):
    #---PRZETWARZA LISTE LINKOW I JE POBIERA---
    if not os.path.exists(target_dir):
        os.makedirs(target_dir)

    print(f"[DOWNLOADER] Rozpoczynanie pobierania {len(links)} plikow")
    
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(download_single_file, url, target_dir) for url in links]
        for future in as_completed(futures):
            result = future.result()
            print(result)
            
    print(f"[DOWNLOADER] Zakonczono pobieranie plikow do: {target_dir}")

#---ADRES WFS GRANIC POWIATOW (PRG) I NAGLOWKI HTTP---
WFS_PRG = 'https://mapy.geoportal.gov.pl/wss/service/PZGIK/PRG/WFS/AdministrativeBoundaries'
HEADERS = {'User-Agent': 'Mozilla/5.0'}

#---UKLADY WSPOLRZEDNYCH ARKUSZA (NAZWA+EPSG) ('uklad_xy' w skorowidzu)---
UKLADY = {'PL-1992': 2180, 'PL-2000:S5': 2176, 'PL-2000:S6': 2177,
          'PL-2000:S7': 2178, 'PL-2000:S8': 2179}


def download_powiaty(cache_dir):
    #---POBIERA GRANICE JPT Z SERWERA WFS LUB WCZYTUJE Z DYSKU---
    powiaty_file = os.path.join(cache_dir, 'JPT_powiat.geojson')

    if os.path.exists(powiaty_file):
        print('[PRG] Wczytano warstwe PRG z cache')
        return gpd.read_file(powiaty_file)

    print('[PRG] Pobieranie warstwy PRG z serwera')
    params_prg = {'service': 'WFS', 'version': '1.1.0', 'request': 'GetFeature',
                  'typeName': 'ms:A02_Granice_powiatow', 'srsName': 'EPSG:2180',
                  'outputFormat': 'text/xml; subType=gml/3.1.1'}
    try:
        req_prg = requests.get(WFS_PRG, params=params_prg, headers=HEADERS, timeout=60)
        if req_prg.status_code != 200:
            print(f'[PRG] Blad serwera PRG: {req_prg.status_code}')
            sys.exit()

        powiaty_gdf = gpd.read_file(io.BytesIO(req_prg.content), engine='fiona')
        powiaty_gdf.to_file(powiaty_file, driver='GeoJSON')
        print(f'[PRG] Warstwa granic pobrana i zapisana w {cache_dir}')
        return powiaty_gdf
    except SystemExit:
        raise
    except Exception as e:
        print(f'[PRG] Blad pobierania granic: {e}')
        sys.exit()


def read_config(config_path, wymagane_klucze=()):
    #---WCZYTANIE JSON: SPR CZY ISTNIEJE, PARSOWANIE, SPRAWDZANIE POPRAWNOSCI KLUCZY---
    config_path = Path(config_path)
    if not config_path.exists():
        print(f'Plik {config_path.name} NIE istnieje w folderze: {config_path.parent}')
        sys.exit()

    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            config_data = json.load(f)
    except json.decoder.JSONDecodeError as je:
        print(f'BLAD PARSOWANIA JSON w {config_path.name}: {je}')
        sys.exit()

    for klucz in wymagane_klucze:
        if not config_data.get(klucz):
            print(f"W pliku {config_path.name} brak klucza '{klucz}'!")
            sys.exit()

    return config_data

def strip_list(wartosc):
    #---OCZYSZCZA LISTE DO POSTACI WCZYTYWANEJ PRZEZ PROGRAM---
    if wartosc is None:
        return []
    if isinstance(wartosc, (list, tuple)):
        return [str(x).strip() for x in wartosc if str(x).strip()]
    return [x.strip() for x in str(wartosc).split(',') if x.strip()]

import zipfile
import os
from pathlib import Path

def unzip(zip_path, target_dir):
    #---WYPAKOWANIE BEZPOSREDNIO DO FOLDEROW---
    with zipfile.ZipFile(zip_path, 'r') as zip_ref:
        for member in zip_ref.infolist():
            if member.is_dir():
                continue
            
            filename = os.path.basename(member.filename)
            if not filename:
                continue
                
            target_path = os.path.join(target_dir, filename)
            
            #podwojne 'with' dla pewnosci zamkniecia wszystkich plikow
            with zip_ref.open(member) as source:
                with open(target_path, "wb") as target:
                    target.write(source.read())
                
    print(f"Wypakowano {os.path.basename(zip_path)} do {target_dir}.")