import geopandas as gpd
import json
import pandas as pd
import folium
import requests
import io
import sys
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from shapely.geometry import box
import branca.colormap as cm
import unicodedata

#---SCIEZKI---
wfs_PRG = 'https://mapy.geoportal.gov.pl/wss/service/PZGIK/PRG/WFS/AdministrativeBoundaries'
wfs_nmtKR = 'https://mapy.geoportal.gov.pl/wss/service/PZGIK/NumerycznyModelTerenuKRON86/WFS/Skorowidze'
wfs_nmt = 'https://mapy.geoportal.gov.pl/wss/service/PZGIK/NumerycznyModelTerenuEVRF2007/WFS/Skorowidze'

NMT_SERVICES = {
    'KRON86': {
        'url': wfs_nmtKR,
        'years': {2000, *range(2004, 2020)},
    },
    'EVRF2007': {
        'url': wfs_nmt,
        'years': set(range(2018, 2026)),
    },
}

config_file = Path(__file__).with_name('config.json')
config = {}
if config_file.exists():
    with open(config_file, 'r', encoding='utf-8') as f:
        config = json.load(f)

map_config = config.get('mapa_folium', {})

def clean_text(txt):
    txt = unicodedata.normalize("NFKD", txt).encode("ascii", "ignore").decode()
    return txt.replace(" ", "_").upper()

def repair_mojibake(txt):
    txt = str(txt)
    for bad, good in {
        '\u0102\u201c': '\u00d3',
        '\u0102\u0142': '\u00f3',
    }.items():
        txt = txt.replace(bad, good)
    try:
        repaired = txt.encode('cp1250').decode('utf-8')
    except UnicodeError:
        return txt
    return repaired if any(ch in txt for ch in 'ĂĹÄ') else txt

def compact_powiat_name(txt):
    txt = repair_mojibake(txt)
    txt = unicodedata.normalize("NFKD", str(txt)).encode("ascii", "ignore").decode()
    txt = re.sub(r'[^0-9a-zA-Z]+', ' ', txt).lower().strip()
    words = [word for word in txt.split() if word not in {'powiat', 'miasto', 'm'}]
    return ''.join(words)

def nmt_sources_for_year(year):
    try:
        year_int = int(year)
    except ValueError:
        return []

    return [
        (name, data['url'])
        for name, data in NMT_SERVICES.items()
        if year_int in data['years']
    ]

def parse_nmt_entry(entry, service_name, powiat_geom):
    data = {'uklad_h_service': service_name}
    for child in entry:
        clean_tag = child.tag.split('}')[-1]
        if child.text and child.text.strip():
            data[clean_tag] = child.text

    lc, uc = None, None
    for sub in entry.iter():
        tag = sub.tag.split('}')[-1]
        if tag == 'lowerCorner':
            lc = sub.text.split()
        elif tag == 'upperCorner':
            uc = sub.text.split()

    if not lc or not uc:
        return None

    x1, y1 = float(lc[0]), float(lc[1])
    x2, y2 = float(uc[0]), float(uc[1])
    geometry_xy = box(x1, y1, x2, y2)
    geometry_yx = box(y1, x1, y2, x2)

    if geometry_xy.intersects(powiat_geom):
        data['geometry'] = geometry_xy
        data['os_geometrii'] = 'xy'
        return data

    if geometry_yx.intersects(powiat_geom):
        data['geometry'] = geometry_yx
        data['os_geometrii'] = 'yx'
        return data

    return None

def nmt_feature_key(data):
    bounds = tuple(round(v, 3) for v in data['geometry'].bounds)
    return (
        data.get('url_do_pobrania', ''),
        data.get('godlo', ''),
        data.get('nr_zglosz', ''),
        bounds,
    )

# do korzystania z serwerow
headers = {'User-Agent': 'Mozilla/5.0'}

#---ZEBY NIE POBIERAC ZA KAZDYM RAZEM WARSTWY POWIATOW---
cache_dir = map_config.get('cache_dir', r'C:\Users\olaa3\Desktop\SKOROWIDZE\cache')
os.makedirs(cache_dir, exist_ok=True)
powiaty_file = os.path.join(cache_dir, 'granice_powiatow.geojson')

def download_powiaty():
    if os.path.exists(powiaty_file):
        print('Wczytano warstwe PRG z cache')
        return gpd.read_file(powiaty_file)

    print('Pobieranie warstwy PRG z serwera')

    params_PRG = {'service': 'WFS',
                  'version': '1.1.0',
                  'request': 'GetFeature',
                  'typeName': 'ms:A02_Granice_powiatow',  #z XML
                  'srsName': 'EPSG:2180',
                  'outputFormat': 'text/xml; subType=gml/3.1.1'}

    try:
        req_prg = requests.get(wfs_PRG, params=params_PRG, headers=headers, timeout=60)

        if req_prg.status_code != 200:
            print(f'Blad serwera PRG: {req_prg.status_code}')
            sys.exit()

        powiaty_gdf = gpd.read_file(io.BytesIO(req_prg.content), engine='fiona')

        powiaty_gdf.to_file(powiaty_file, driver='GeoJSON')
        print(f'Warstwa granic pobrana i zapisana w {cache_dir}')

        return powiaty_gdf
    except Exception as e:
        print(f'Blad pobierania granic: {e}')
        sys.exit()

#---FILTROWANIE POWIATU---
config_powiat = map_config.get('powiat')
config_years = map_config.get('years')

if config_powiat and config_powiat.strip():
    nazwa_user = config_powiat.strip()
    print(f'Powiat ustawiony z config.json: {nazwa_user}')
else:
    print('\nBrak nazwy powiatu w config.json. Podaj nazwe powiatu:')
    nazwa_user = input().strip()
    if not nazwa_user:
        print('Nie podano nazwy powiatu. Uzupełnij pole "powiat" w config.json lub uruchom ponownie.')
        sys.exit(1)

if config_years is not None:
    if isinstance(config_years, list):
        year_list = []
        for item in config_years:
            item_str = str(item).strip()
            if '-' in item_str:
                try:
                    parts = item_str.split('-')
                    if len(parts) == 2:
                        year_from = int(parts[0].strip())
                        year_to = int(parts[1].strip())
                        if year_from > year_to:
                            print(f'Błąd: zakres lat {item_str} - lewa liczba ({year_from}) jest większa od prawej ({year_to})')
                            sys.exit(1)
                        year_list.extend([str(y) for y in range(year_from, year_to + 1)])
                    else:
                        year_list.append(item_str)
                except ValueError:
                    print(f'Błąd: nie można sparsować zakresu lat {item_str}')
                    sys.exit(1)
            else:
                year_list.append(item_str)
        year_user = year_list
    else:
        year_user = [l.strip() for l in str(config_years).split(',')]
    print(f'Rok(y) ustawione z config.json: {", ".join(year_user)}')
else:
    print('\nPodaj rok (lub lata, oddzielone przecinkiem):')
    year_user = [l.strip() for l in input().split(',')]

powiaty = download_powiaty()

#Filtracja powiatu wg kolumny JPT_NAZWA_
#sprowadza np. "powiat Rzeszów" i "rzeszow" do tego samego klucza
powiat_key = compact_powiat_name(nazwa_user)
powiat_names = powiaty['JPT_NAZWA_'].apply(compact_powiat_name)
powiat_test = powiaty[powiat_names == powiat_key].copy()

if powiat_test.empty:
    print(f'Powiat {nazwa_user} nie istnieje')
    sys.exit()

selected_powiat_file = os.path.join(cache_dir, f'wybrany_powiat_{clean_text(nazwa_user)}.geojson')
powiat_test.to_file(selected_powiat_file, driver='GeoJSON')
print(f'Wyselekcjonowany powiat zapisany w: {selected_powiat_file}')


#---!!!---
#---CZESC POBIERANIA NMT---
#---!!!---


#Obliczenie BBOX dla powiatu
minx, miny, maxx, maxy = powiat_test.total_bounds
bbox_xy_str = f'{minx},{miny},{maxx},{maxy}'
bbox_yx_str = f'{miny},{minx},{maxy},{maxx}'
print(f'BBOX powiatu EPSG:2180 (x,y): {minx},{miny},{maxx},{maxy}')
print(f'BBOX WFS wariant x,y: {bbox_xy_str}')
print(f'BBOX WFS wariant y,x: {bbox_yx_str}')

#Obliczanie srodka mapy (bezpiecznie na 2180)
centroid_2180 = powiat_test.geometry.centroid.iloc[0]
c_gdf = gpd.GeoDataFrame(geometry=[centroid_2180], crs="EPSG:2180").to_crs(epsg=4326)
c_lat, c_lon = c_gdf.geometry.y.iloc[0], c_gdf.geometry.x.iloc[0]

#Tutaj musi byc WGS bo Folium wysiadzie
#skorowidze_4326 = skorowidze.to_crs(epsg=4326)
powiat_4326 = powiat_test.to_crs(epsg=4326)
powiaty_4326 = powiaty.to_crs(epsg=4326)

#KONWERSJA KOLUMN NA TEST (Tez musi byc do duzo plikow nie przyjmuje Timestampow)
for df in [powiat_4326, powiaty_4326]:
    for col in df.columns:
        if col != 'geometry':
            df[col] = df[col].apply(lambda x: str(x) if x is not None else "")

# --- 6. GENEROWANIE MAPY FOLIUM ---
#przyrostek 4326 dla zachowania odpowiedniego ukladu wspolrzednych dla Folium
print('Tworzenie mapy')
mapa = folium.Map(location=[c_lat, c_lon], zoom_start=11, tiles="CartoDB positron")

#---WSZYSTKIE POWIATY, podklad---
folium.GeoJson(powiaty_4326, name='Wszystkie powiaty',
               style_function=lambda x: {'color': 'grey', 'fillOpacity': 0,
                                         'dashArray': '5, 5', 'weight': 1}).add_to(mapa)
    
#---WYBRANY POWIAT---
folium.GeoJson(powiat_4326, name='Wybrany powiat',
               style_function=lambda x: {'color': 'red', 'fillOpacity': 0,
                                         'weight': 4}).add_to(mapa)
    
#---PETLA PO LATACH, DODAWNIE WARSTW---
#---POBIERANIE ARKUSZY NMT---
for year in year_user:
    print(f'\nPobieranie danych dla roku {year}')
    layer_name = f'gugik:SkorowidzNMT{year}'
    nmt_sources = nmt_sources_for_year(year)

    if not nmt_sources:
        print(f'Rok {year} nie wystepuje w znanych uslugach NMT KRON86/EVRF2007')
        continue

    year_features_data = []
    seen_features = set()

    for service_name, service_url in nmt_sources:
        print(f'Sprawdzanie uslugi {service_name}')
        for bbox_order, bbox_str in [('xy', bbox_xy_str), ('yx', bbox_yx_str)]:
            print(f'  Zapytanie BBOX {bbox_order}')

            params_nmt = {
                'service': 'WFS',
                'version': '1.1.0',
                'request': 'GetFeature',
                'typeName': layer_name,
                'outputFormat': 'text/xml; subType=gml/3.1.1',
                'srsName': 'EPSG:2180',
                'bbox': bbox_str
            }

            try:
                response = requests.get(service_url, params=params_nmt, headers=headers, timeout=60)
                if response.status_code != 200:
                    print(f'Blad serwera NMT {service_name} dla roku {year}: {response.status_code}')
                    continue

                root = ET.fromstring(response.content)
                raw_count = 0
                matched_count = 0

                for entry in root.iter():
                    if 'SkorowidzNMT' in entry.tag:
                        raw_count += 1
                        data = parse_nmt_entry(entry, service_name, powiat_test.geometry.iloc[0])
                        if data is None:
                            continue

                        data['bbox_zapytania'] = bbox_order
                        feature_key = nmt_feature_key(data)
                        if feature_key in seen_features:
                            continue

                        seen_features.add(feature_key)
                        year_features_data.append(data)
                        matched_count += 1

                print(f'  Odpowiedz: {raw_count} obiektow, pasuje po geometrii: {matched_count}')
            except Exception as e:
                print(f'Blad podczas pobierania roku {year} z uslugi {service_name}, BBOX {bbox_order}: {e}')

    try:
        if not year_features_data:
            print(f'Brak arkuszy NMT dla roku {year} w tym obszarze')
            continue

        #pobrane dane dla dla danego roku
        skorowidze_kwadrat = gpd.GeoDataFrame(year_features_data, crs="EPSG:2180")
        
        #przeciecie elementow
        skorowidze = gpd.sjoin(skorowidze_kwadrat, powiat_test, predicate='intersects')
        
        if skorowidze.empty:
            print(f'Po filtracji przestrzennej brak arkuszy dla roku {year}')
            continue

        selected_nmt_file = os.path.join(cache_dir, f'skorowidze_NMT_{clean_text(nazwa_user)}_{year}.geojson')
        skorowidze.to_file(selected_nmt_file, driver='GeoJSON')
        print(f'Wyselekcjonowane skorowidze NMT zapisane w: {selected_nmt_file}')

        print(f'Pobrano {len(skorowidze)} arkuszy dla roku {year}')

        # Wyswietlanie listy godel w konsoli
        print(f"Lista kampanii pomiarowych w {year} (nr zgloszenia):")
        lista_id = sorted(skorowidze['nr_zglosz'].unique().tolist())
        for i in range(0, len(lista_id), 5):
            print(", ".join(lista_id[i:i+5]))

        skorowidze_4326 = skorowidze.to_crs(epsg=4326)

        #Folium/JSON nie obsluguje typow specjalnych
        for col in skorowidze_4326.columns:
            if col != 'geometry':
                skorowidze_4326[col] = skorowidze_4326[col].apply(lambda x: str(x) if x is not None else "")

        # --- GENEROWANIE WARSTW DLA KAMPANII W DANYM ROKU ---
        nmt_kampania = skorowidze_4326['nr_zglosz'].unique()
        
        #https://python-visualization.github.io/folium/latest/advanced_guide/colormaps.html
        colormap = cm.linear.Paired_08.scale(0, len(nmt_kampania))

        for i, n in enumerate(nmt_kampania):
            #filtracja po konkretnym numerze zgloszenia
            nr_kampanii = skorowidze_4326[skorowidze_4326['nr_zglosz'] == n]
            color_hex = colormap(i) 
            
            #grupa roku i nr zgloszenia
            warstwa_zgloszenie = folium.FeatureGroup(name=f'[{year}] Zgloszenie {n}')

            folium.GeoJson(
                nr_kampanii,
                style_function=lambda x, k=color_hex: {
                    'fillColor': k, 
                    'color': k,
                    'weight': 1,
                    'fillOpacity': 0.4
                },
                tooltip=folium.GeoJsonTooltip(
                    fields=['akt_data', 'godlo', 'format', 'nr_zglosz', 'uklad_xy', 'char_przestrz', 'uklad_h', 'uklad_h_service', 'bbox_zapytania', 'os_geometrii', 'blad_sr_wys', 'zrodlo_danych'],
                    aliases=['Data:', 'Arkusz:', 'Format:', 'Numer zgloszenia:', 'Uklad wspolrzednych:', 'Rozdzielczosc:', 'Uklad wys.:', 'Usluga:', 'BBOX:', 'Os geometrii:', 'Blad wys.:', 'zrodlo:']
                ),
                popup=folium.GeoJsonPopup(
                    fields=['url_do_pobrania'],
                    aliases=['Link do pobrania:']
                )
            ).add_to(warstwa_zgloszenie)
            
            #dodanie do glownej pamy
            warstwa_zgloszenie.add_to(mapa)

    except Exception as e:
        print(f'Blad podczas przetwarzania roku {year}: {e}')

#---ZAPIS---
#rozwiniety panel sterowania warstwami
folium.LayerControl(collapsed=False).add_to(mapa)

#budowa nazwy pliku i zapis
powiat_clean = clean_text(nazwa_user)

# Sprawdzenie czy lata tworzą ciągły zakres
try:
    years_int = sorted([int(y) for y in year_user])
    if len(years_int) > 1 and years_int[-1] - years_int[0] == len(years_int) - 1:
        # Ciągły zakres
        years_str = f"{years_int[0]}-{years_int[-1]}"
    else:
        # Niepełny zakres lub pojedyncze lata
        years_str = "_".join([str(y) for y in years_int])
except (ValueError, TypeError):
    years_str = "_".join(year_user)

base_name = map_config.get('output_name', 'wynik_nmt_.html')
name, ext = os.path.splitext(base_name)

final_name = f"{name}{powiat_clean}_{years_str}{ext}"
save_dir = os.path.join(cache_dir, final_name)

# Ostateczny zapis mapy
mapa.save(save_dir)

print(f'Mapa zapisana w: {save_dir}')
