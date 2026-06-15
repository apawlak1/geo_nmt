import geopandas as gpd
import pandas as pd
import folium
import requests
import io
import sys
import os
import xml.etree.ElementTree as ET
from shapely.geometry import box
import branca.colormap as cm
from processor import process_data  #moj plik .py
from downloader import download_nmt_files   #moj plik .py

#---SCIEZKI---
wfs_PRG = 'https://mapy.geoportal.gov.pl/wss/service/PZGIK/PRG/WFS/AdministrativeBoundaries'
wfs_nmtKR = 'https://mapy.geoportal.gov.pl/wss/service/PZGIK/NumerycznyModelTerenuKRON86/WFS/Skorowidze'
wfs_nmt = 'https://mapy.geoportal.gov.pl/wss/service/PZGIK/NumerycznyModelTerenuEVRF2007/WFS/Skorowidze'

headers = {'User-Agent': 'Mozilla/5.0'}

cache_dir = r'C:\Users\olaa3\Desktop\SKOROWIDZE\cache'
powiaty_file = os.path.join(cache_dir, 'sopot.geojson')

dane_do_pobrania={}

def download_powiaty():
    if os.path.exists(powiaty_file):
        print('Wczytano warstwe PRG z cache')
        return gpd.read_file(powiaty_file)
    
    print('Pobieranie warstwy PRG z serwera')
    params_PRG = {'service': 'WFS',
                  'version': '1.1.0',
                  'request': 'GetFeature',
                  'typeName': 'ms:A02_Granice_powiatow', 
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

powiaty = download_powiaty()

print('\nPodaj nazwe powiatu:')
nazwa_user = input().strip()

print('\nPodaj rok (lub lata, oddzielone przecinkiem):')
year_user = [l.strip() for l in input().split(',')]

#!!!W TYM MIEJSCU W ROZNYCH JPT ROZNA NAZWA KOLUMNY, MOZNA TO OPRACOWAC
powiat_test = powiaty[powiaty['JPT_NAZWA_'].str.contains(rf'\b{nazwa_user}\b', case=False, regex=True)].copy()

if powiat_test.empty:
    print(f'Powiat {nazwa_user} nie istnieje')
    sys.exit()

powiat_save = powiat_test['JPT_NAZWA_'].iloc[0].replace(' ', '_')
lata_save = '_'.join(year_user)

#Obliczenie BBOX dla powiatu
minx, miny, maxx, maxy = powiat_test.total_bounds
bbox_str = f'{miny},{minx},{maxy},{maxx}'

#Obliczanie srodka mapy
centroid_2180 = powiat_test.geometry.centroid.iloc[0]
c_gdf = gpd.GeoDataFrame(geometry=[centroid_2180], crs='EPSG:2180').to_crs(epsg=4326)
c_lat, c_lon = c_gdf.geometry.y.iloc[0], c_gdf.geometry.x.iloc[0]

powiat_4326 = powiat_test.to_crs(epsg=4326)
powiaty_4326 = powiaty.to_crs(epsg=4326)

for df in [powiat_4326, powiaty_4326]:
    for col in df.columns:
        if col != 'geometry':
            df[col] = df[col].apply(lambda x: str(x) if x is not None else '')

print(f'\nTworzenie mapy HTML')
mapa = folium.Map(location=[c_lat, c_lon], zoom_start=11, tiles='CartoDB positron')

folium.GeoJson(powiaty_4326, name='Wszystkie powiaty',
               style_function=lambda x: {'color': 'grey', 'fillOpacity': 0, 'dashArray': '5, 5', 'weight': 1}).add_to(mapa)
    
folium.GeoJson(powiat_4326, name='Wybrany powiat',
               style_function=lambda x: {'color': 'red', 'fillOpacity': 0, 'weight': 4}).add_to(mapa)
    
for year in year_user:
    print(f'\nPobieranie danych dla roku {year}')
    layer_name = f'gugik:SkorowidzNMT{year}'

    params_nmt = {'service': 'WFS',
                  'version': '1.0.0', 
                  'request': 'GetFeature',
                  'typeName': layer_name,
                  'outputFormat': 'text/xml; subType=gml/3.1.1',
                  'bbox': f'{minx},{miny},{maxx},{maxy}'}

    #TUTAJ FRAGMENT ROBOCZY, MIALAM BLAD I MUSIALAM SPRAWDZIC
    try:
        response = requests.get(wfs_nmt, params=params_nmt, headers=headers, timeout=60)
        
        if response.status_code == 200:
            print(f'[TEST] Serwer odpowiedzial prawidlowo, rozmiar odp: {len(response.content)} bajtów.')
        else:
            print(f'[TEST] Serwer zwrocil kod bledu: {response.status_code}')
            
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

        skorowidze_kwadrat = gpd.GeoDataFrame(features_data, crs='EPSG:2180')
        skorowidze = gpd.sjoin(skorowidze_kwadrat, powiat_test, predicate='intersects')
        
        if skorowidze.empty:
            print(f'Po filtracji brak arkuszy dla roku {year}')
            continue

        if not skorowidze.empty:
            #---TUTAJ WAZNA ZMIANA, POBIERAM TEZ CRS---

            #slownik wartosci
            uklady={'PL-1992': 2180,
                    'PL-2000:S5': 2176,
                    'PL-2000:S6': 2177,
                    'PL-2000:S7':2178,
                    'PL-2000:S8':2179}

            ldp=[]
            for _, row in skorowidze.iterrows():
                uklad=str(row.get('uklad_xy', '')).strip()

                epsg=uklady.get(uklad, 2180)    #2180 daje na w razie czego, raczej nie ma takiej sytuacji

                ldp.append({'url': row['url_do_pobrania'],
                            'epsg': epsg})

            dane_do_pobrania[year]={'linki': ldp,
                                    'folder': os.path.join(cache_dir, f'nmt_{year}_{powiat_save}')}
            
        print(f'Znaleziono {len(skorowidze)} arkuszy dla roku {year}')

        skorowidze_4326 = skorowidze.to_crs(epsg=4326)
        print(f'Lista kampanii pomiarowych w {year} (nr zgloszenia), ID i format:')
        info = skorowidze[['nr_zglosz', 'format']].drop_duplicates()
        for _, row in info.iterrows():
            print(f" - Zgloszenie: {row['nr_zglosz']} | Format: {row['format']}")

        skorowidze_4326 = skorowidze.to_crs(epsg=4326)

        for col in skorowidze_4326.columns:
            if col != 'geometry':
                skorowidze_4326[col] = skorowidze_4326[col].apply(lambda x: str(x) if x is not None else '')

        nmt_kampania = skorowidze_4326['nr_zglosz'].unique()
        colormap = cm.linear.Paired_08.scale(0, max(2, len(nmt_kampania)))

        for i, n in enumerate(nmt_kampania):
            nr_kampanii = skorowidze_4326[skorowidze_4326['nr_zglosz'] == n]
            color_hex = colormap(i) 
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

            #dodanie do glownej mapy
            warstwa_zgloszenie.add_to(mapa)

    except Exception as e:
        print(f'Blad podczas przetwarzania roku {year}: {e}')

folium.LayerControl(collapsed=False).add_to(mapa)

nazwa_save = f'wynik_nmt_{powiat_save}_{lata_save}.html'
save_dir = os.path.join(cache_dir, nazwa_save)

mapa.save(save_dir)
print(f'Mapa zapisana w: {save_dir}')

# ---POBIERANIE I PRZETWARZANIE---
if not dane_do_pobrania:
    print('Brak danych do pobrania. n\ZAKONCZONO')
else:
    for year, info in dane_do_pobrania.items():
        liczba = len(info['linki'])
        folder_year=info['folder']
        os.makedirs(folder_year, exist_ok=True)

        #zawsze pytam o mozaike, zapisuje decyzje
        while True:
            decyzja_moz=input(f'\nPolaczyc arkusze w mozaike dla roku {year}? (t/n): ').lower().strip()
            if decyzja_moz in ['t', 'n']:
                create_mosaic=(decyzja_moz=='t')
                break
            print(f'Wpisz [t] dla TAK lub [n] dla NIE.')
            
        #zawsze pytam o pobieranie
        while True:
            decyzja_downl=input(f'Pobrac {liczba} arkuszy dla roku {year}? (t/n): ').lower().strip()
            if decyzja_downl in ['t', 'n']:
                do_download=(decyzja_downl=='t')
                break
            print(f'Wpisz [t] dla TAK lub [n] dla NIE.')

        #WYKONANIE AKCJI
        folder_rok = info['folder']
        os.makedirs(folder_rok, exist_ok=True)
        
        #pobieranie tylko jesli uzytkownik chcial
        if do_download:
            print(f'\n---POBIERANIE DANYCH DLA ROKU {year}---')
            download_nmt_files(info['linki'], folder_rok)

        #pliki_na_dysku = [f for f in os.listdir(folder_rok) if f.lower().endswith(('.asc', '.xyz', '.txt'))]
            
        #if len(pliki_na_dysku) > 0:
            print(f'--- PRZETWARZANIE DANYCH DLA ROKU {year} ---')
            geom_2180 = powiat_test.to_crs(epsg=2180).geometry.iloc[0] #wymuszam w razie czego 2180
            nazwa_pliku = f'NMT_{powiat_save}_{year}_FINAL.tif' 
            pelna_sciezka_wyniku = os.path.join(folder_rok, nazwa_pliku)

            #slownik powiazan zeby przetransportowac te CRSy
            mapa_uklady={}
            for p in info['linki']:
                nazwa_pliku = p['url'].split('/')[-1]
                mapa_uklady[nazwa_pliku] = p['epsg']

            #to samo dla akt_data
            mapa_daty={row['url_do_pobrania'].split('/')[-1]:
                       pd.to_datetime(row['akt_data'])
                       for index, row in skorowidze.iterrows()}


            #WYWOLANIE FUNKCJI Z DECYZJA O MOZAICE
            process_data(folder_rok, pelna_sciezka_wyniku, geom_2180, mapa_uklady, mapa_daty, create_mosaic=create_mosaic, extract=do_download)
                
            #informacja w zaleznosci od trybu
            if create_mosaic:
                print(f'Mozaika zapisana w: {pelna_sciezka_wyniku}')
            else:
                print(f'Kafelki zapisano w folderze: {os.path.join(os.path.dirname(pelna_sciezka_wyniku), "wyniki_konwersji")}')

print('\nZAKONCZONO')