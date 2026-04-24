import geopandas as gpd
import json
import pandas as pd
import folium
import requests
import io
import sys
import os
import xml.etree.ElementTree as ET
from pathlib import Path
from shapely.geometry import box
import branca.colormap as cm

#---SCIEZKI---
wfs_PRG = 'https://mapy.geoportal.gov.pl/wss/service/PZGIK/PRG/WFS/AdministrativeBoundaries'
wfs_nmtKR = 'https://mapy.geoportal.gov.pl/wss/service/PZGIK/NumerycznyModelTerenuKRON86/WFS/Skorowidze'
wfs_nmt = 'https://mapy.geoportal.gov.pl/wss/service/PZGIK/NumerycznyModelTerenuEVRF2007/WFS/Skorowidze'

config_file = Path(__file__).with_name('config.json')
config = {}
if config_file.exists():
    with open(config_file, 'r', encoding='utf-8') as f:
        config = json.load(f)

map_config = config.get('mapa_folium', {})

# do korzystania z serwerow
headers = {'User-Agent': 'Mozilla/5.0'}

#---ZEBY NIE POBIERAC ZA KAZDYM RAZEM WARSTWY POWIATOW---
cache_dir = map_config.get('cache_dir', r'C:\Users\olaa3\Desktop\SKOROWIDZE\cache')
powiaty_file = os.path.join(cache_dir, 'granice_powiatow.geojson')

def download_powiaty():
    #sprawdzam czy plik istnieje w cache, jak nie to pobieram
    if os.path.exists(powiaty_file):
        print('Wczytano warstwe PRG z cache')
        return gpd.read_file(powiaty_file)
    
    #jak nie istnieje to pobieram
    print('Pobieranie warstwy PRG z serwera')

    # ---PODSZYWANIE SIE POD PRZEGLADARKE---
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

        #zapisuje do cache na przyszlosc
        powiaty_gdf.to_file(powiaty_file, driver='GeoJSON')
        print(f'Warstwa granic pobrana i zapisana w {cache_dir}')

        return powiaty_gdf
    
    except Exception as e:
        print(f'Blad pobierania granic: {e}')
        sys.exit()

powiaty = download_powiaty()

#---FILTROWANIE POWIATU---
config_powiat = map_config.get('powiat')
config_years = map_config.get('years')

if config_powiat:
    nazwa_user = config_powiat.strip()
    print(f'Powiat ustawiony z config.json: {nazwa_user}')
else:
    print('\nPodaj nazwe powiatu:')
    nazwa_user = input().strip()

if config_years is not None:
    if isinstance(config_years, list):
        year_user = [str(y).strip() for y in config_years]
    else:
        year_user = [l.strip() for l in str(config_years).split(',')]
    print(f'Rok(y) ustawione z config.json: {", ".join(year_user)}')
else:
    print('\nPodaj rok (lub lata, oddzielone przecinkiem):')
    year_user = [l.strip() for l in input().split(',')]

#Filtracja powiatu wg kolumny JPT_NAZWA_
#regex zabezpiecza przed wyszukaniem np. OSTRZESZOWSKIEGO przy szukaniu RZESZOWSKIEGO
powiat_test = powiaty[powiaty['JPT_NAZWA_'].str.contains(rf'\b{nazwa_user}\b', case=False, regex=True)].copy()

if powiat_test.empty:
    print(f'Powiat {nazwa_user} nie istnieje')
    sys.exit()


#---!!!---
#---CZESC POBIERANIA NMT---
#---!!!---


#Obliczenie BBOX dla powiatu
minx, miny, maxx, maxy = powiat_test.total_bounds
bbox_str = f'{miny},{minx},{maxy},{maxx}'

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
        response = requests.get(wfs_nmt, params=params_nmt, headers=headers, timeout=60)
        root = ET.fromstring(response.content)
        features_data = []
    
        for entry in root.iter():
            if 'SkorowidzNMT' in entry.tag:
                data = {}
                for child in entry:
                    clean_tag = child.tag.split('}')[-1]
                    if child.text and child.text.strip():
                        data[clean_tag] = child.text
                
                lc, uc = None, None
                for sub in entry.iter():
                    if 'lowerCorner' in sub.tag: lc = sub.text.split()
                    elif 'upperCorner' in sub.tag: uc = sub.text.split()
                
                if lc and uc:
                    data['geometry'] = box(float(lc[0]), float(lc[1]), float(uc[0]), float(uc[1]))
                    features_data.append(data)

        if not features_data:
            print(f'Brak arkuszy NMT dla roku {year} w tym obszarze')
            continue

        #pobrane dane dla dla danego roku
        skorowidze_kwadrat = gpd.GeoDataFrame(features_data, crs="EPSG:2180")
        
        #przeciecie elementow
        skorowidze = gpd.sjoin(skorowidze_kwadrat, powiat_test, predicate='intersects')
        
        if skorowidze.empty:
            print(f'Po filtracji przestrzennej brak arkuszy dla roku {year}')
            continue

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
                    fields=['akt_data', 'godlo', 'format', 'nr_zglosz', 'uklad_xy', 'char_przestrz', 'uklad_h', 'blad_sr_wys', 'zrodlo_danych'],
                    aliases=['Data:', 'Arkusz:', 'Format:', 'Numer zgloszenia:', 'Uklad wspolrzednych:', 'Rozdzielczosc:', 'Uklad wys.:', 'Blad wys.:', 'zrodlo:']
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
powiat_save = powiat_test['JPT_NAZWA_'].iloc[0].replace(" ", "_")
lata_save = "_".join(year_user)
output_name = map_config.get('output_name', f'wynik_nmt_{powiat_save}_{lata_save}.html')
save_dir = os.path.join(cache_dir, output_name)

# Ostateczny zapis mapy
mapa.save(save_dir)

print(f'Mapa zapisana w: {save_dir}')