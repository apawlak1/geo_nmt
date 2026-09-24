try:
    import geopandas as gpd
    import pandas as pd
    import folium
    import requests
    import sys
    import os
    import xml.etree.ElementTree as ET
    from shapely.geometry import box
    import branca.colormap as cm
    from processor import process_data  #moj plik .py
    from downloader import download_nmt_files, download_powiaty, UKLADY, read_config  #moj plik .py
    from zarr_magazyn import Magazyn, waliduj_poziomy  #moj plik .py
    from zarr_aktualizacja import zaplanuj, aktualizuj_rok_zarr  #moj plik .py
    from zarr_poziomy import nazwa_metody  #moj plik .py
    from datetime import date
    from podklady_mapy import dodaj_podklady  #moj plik .py
    from mozaika_geotiff import zbuduj_mozaike_geotiff  #moj plik .py
    from nfp_mosaics import find_date  #moj plik .py
    from pathlib import Path

    BLOK_PX=2048             #rozmiar bloku/chunku ZARR w pikselach
    ZARR_FORMAT=2            #format Zarr 2 = najszersza zgodnosc z GDAL/QGIS/ArcGIS
    WATKI_POBIERANIA=3       #ile plikow pobierac naraz
    PROBY_POBIERANIA=3       #ile razy probowac pobrac plik przy bledzie/timeout
    TIMEOUT_POLACZENIA=15    #limit nawiazania polaczenia [s]
    TIMEOUT_ODCZYTU=180      #limit oczekiwania na dane [s]
    PODKLAD_MAPY='esri'      #podklad mapy HTML: esri / esri_satelita / osm / carto
    CARTO_KEY=None           #klucz CARTO - tylko gdy PODKLAD_MAPY='carto'

    #---WCZYTANIE SCIEZKI CACHE Z CONFIG (wspolna funkcja z downloader.py, patrz tez wersja_dev.py)---
    #szukam w tym samym folderze co skrypty (bezpieczniej)
    script_dir=Path(__file__).resolve().parent
    config_path=script_dir / 'config.json'

    config_data=read_config(config_path, wymagane_klucze=('cache_dir',))
    cache_dir=config_data['cache_dir']
    os.makedirs(cache_dir, exist_ok=True)

    #---config'geo_nmt' (powiat, lata, rozdzielczosc, poziomy, mozaika, pobierz)---
    #jesli klucza brak/jest pusty skrypt pyta o input())
    #config obsluguje skrypt automatycznie lub recznie
    folium_config=config_data.get('geo_nmt', {})
    conf_powiat=folium_config.get('powiat')
    conf_yr=folium_config.get('lata')  #lista, tekst "2019,2020" albo "auto" (od rok_start do biezacego roku)
    conf_cellsize=folium_config.get('rozdzielczosc')  #rozdzielczosc BAZOWA w ZARR [m]; None=najdrobniejszy piksel arkuszy; ZAMRAZANA przy tworzeniu store'u
    conf_poziomy=[float(x) for x in folium_config.get('poziomy', [])]  #poziomy pochodne [m], np. [5, 10, 20]
    conf_resampling=folium_config.get('resampling_poziomow', 'bilinear')  #nearest/bilinear/cubic/average
    conf_mosaic=folium_config.get('zarr')      #True/False -> zapis do ZARR bez pytania t/n, None -> pyta jak dawniej
    conf_geotiff=bool(folium_config.get('geotiff', False))   #True -> zapisz TEZ mozaike jako pojedynczy GeoTIFF (niezaleznie od ZARR)
    conf_download=folium_config.get('pobierz')      #True/False -> pomija pytanie t/n, None -> pyta jak dawniej
    conf_konwersja=bool(folium_config.get('konwersja', True))   #False -> tylko POBIERANIE surowych plikow (bez rozpakowania, konwersji i ZARR)
    conf_wymus=bool(folium_config.get('wymus', False))   #True -> przebuduj warstwy nawet jesli skorowidz sie nie zmienil
    conf_rok_start=int(folium_config.get('rok_start', 2018))   #dla "lata": "auto"

    #---sekcja 'mapa' w config.json: TYLKO wlacznik/wylacznik; podklad jest stalym PODKLAD_MAPY (patrz wyzej)---
    mapa_config=config_data.get('mapa', {})
    conf_mapa_zapisz=bool(mapa_config.get('zapisz', True))   #False -> nie buduje ani nie zapisuje mapy HTML (szybszy start)


    #---WALIDACJA POZIOMOW/METODY OD RAZU (zanim ruszy pobieranie)---
    try:
        conf_resampling=nazwa_metody(conf_resampling)
        if conf_cellsize:
            waliduj_poziomy(float(conf_cellsize), conf_poziomy)
    except ValueError as ve:
        print(f'BLAD w config.json: {ve}')
        sys.exit()
    #---TRYB 'TYLKO POBIERANIE': konwersja wylaczona => brak ZARR (ZARR buduje sie z przekonwertowanych arkuszy)---
    if not conf_konwersja:
        if conf_mosaic:
            print("[UWAGA] 'zarr': true wymaga konwersji arkuszy - przy 'konwersja': false zapis do ZARR jest pomijany.")
        if conf_geotiff:
            print("[UWAGA] 'geotiff': true wymaga konwersji arkuszy - przy 'konwersja': false zapis mozaiki GeoTIFF jest pomijany.")
        conf_mosaic=False
        conf_geotiff=False
        print("Tryb: TYLKO POBIERANIE surowych plikow ('konwersja': false)")

    #---DWA WYKLUCZAJACE SIE TRYBY: 'zarr' (wieloczasowa baza, jak dotad) ALBO 'geotiff' (pojedyncza mozaika
    #na rok, bez wymiaru czasu i bez poziomow) - nigdy oba naraz. 'geotiff': true ZAWSZE wylacza ZARR (bez
    #pytania), a jawne 'zarr': true razem z 'geotiff': true jest bledem konfiguracji. Sprawdzane PO powyzszym
    #wylaczeniu przy 'konwersja': false, zeby ten tryb nie wywolywal falszywego konfliktu.---
    if conf_geotiff and conf_mosaic:
        print("BLAD w config.json: 'zarr' i 'geotiff' wykluczaja sie - ustaw tylko jeden z nich na true.")
        sys.exit()
    if conf_geotiff:
        conf_mosaic=False   #tryb geotiff: bez pytania o ZARR, 'poziomy'/'resampling_poziomow'/'wymus' sa ignorowane
    print(f'ZARR: rozdzielczosc bazowa={conf_cellsize or "auto"} | poziomy={conf_poziomy or "brak"} | '
          f'metoda poziomow={conf_resampling}')
    if conf_geotiff:
        print(f"GeoTIFF: mozaika jednoczasowa (bez poziomow pochodnych), rozdzielczosc="
              f'{conf_cellsize or "auto (najdrobniejszy piksel arkuszy danego roku)"}')


    #---SCIEZKI---
    wfs_nmtKR='https://mapy.geoportal.gov.pl/wss/service/PZGIK/NumerycznyModelTerenuKRON86/WFS/Skorowidze'
    wfs_nmt='https://mapy.geoportal.gov.pl/wss/service/PZGIK/NumerycznyModelTerenuEVRF2007/WFS/Skorowidze'

    headers={'User-Agent': 'Mozilla/5.0'}

    dane_do_pobrania={}

    #---GRANICE POWIATOW---
    powiaty=download_powiaty(cache_dir)

    if conf_powiat and str(conf_powiat).strip():
        nazwa_user=str(conf_powiat).strip()
        print(f'Powiat ustawiony z config.json: {nazwa_user}')
    else:
        print('\nBrak "powiat" pliku config.json. Podaj nazwe powiatu:')
        nazwa_user=input().strip()
        if not nazwa_user:
            print('Nie podano nazwy powiatu. Uzupelnij pole "powiat" w config.json lub uruchom ponownie.')
            sys.exit()

    #---BLAD: str(conf_yr).split(',') NIE DZIALA POPRAWNIE, GDY conf_yr JEST LISTA---
    if isinstance(conf_yr, (list, tuple)):
        year_user=[str(l).strip() for l in conf_yr if str(l).strip()]
    elif isinstance(conf_yr, str) and conf_yr.strip().lower() == 'auto':
        year_user=[str(r) for r in range(conf_rok_start, date.today().year + 1)]
    elif conf_yr is not None and str(conf_yr).strip():
        year_user=[l.strip() for l in str(conf_yr).split(',') if l.strip()]
    else:
        year_user=[]

    if year_user:
        print(f'Rok(i) ustawione z config.json: {", ".join(year_user)}')
    else:
        print('\nBrak "lata" w pliku config.json. Podaj rok (lub lata, oddzielone przecinkiem):')
        year_user=[l.strip() for l in input().split(',') if l.strip()]

    powiat_test=powiaty[powiaty['JPT_NAZWA_'].str.contains(rf'\b{nazwa_user}\b', case=False, regex=True)].copy()

    if powiat_test.empty:
        print(f'Powiat {nazwa_user} nie istnieje')
        sys.exit()

    powiat_save=powiat_test['JPT_NAZWA_'].iloc[0].replace(' ', '_')
    lata_save='_'.join(year_user)

    #Obliczenie BBOX dla powiatu
    minx, miny, maxx, maxy=powiat_test.total_bounds
    bbox_str=f'{miny},{minx},{maxy},{maxx}'

    #Obliczanie srodka mapy
    centroid_2180=powiat_test.geometry.centroid.iloc[0]
    c_gdf=gpd.GeoDataFrame(geometry=[centroid_2180], crs='EPSG:2180').to_crs(epsg=4326)
    c_lat, c_lon=c_gdf.geometry.y.iloc[0], c_gdf.geometry.x.iloc[0]

    mapa=None
    if conf_mapa_zapisz:
        powiat_4326=powiat_test.to_crs(epsg=4326)
        powiaty_4326=powiaty.to_crs(epsg=4326)

        for df in [powiat_4326, powiaty_4326]:
            for col in df.columns:
                if col != 'geometry':
                    df[col]=df[col].apply(lambda x: str(x) if x is not None else '')

        print(f'\nTworzenie mapy HTML')
        
        #ZMIANA: 'CartoDB positron' pokazuje znak wodny "API KEY REQUIRED" (CARTO wymaga teraz klucza) -
        #podklady sa w podklady_mapy.py, wybor w config.json: "mapa": {"podklad": "esri", "carto_key": null}
        mapa=folium.Map(location=[c_lat, c_lon], zoom_start=11, tiles=None)
        dodaj_podklady(mapa, PODKLAD_MAPY, CARTO_KEY)

        folium.GeoJson(powiaty_4326, name='Wszystkie powiaty',
                       style_function=lambda x: {'color': 'grey', 'fillOpacity': 0, 'dashArray': '5, 5', 'weight': 1}).add_to(mapa)

        folium.GeoJson(powiat_4326, name='Wybrany powiat',
                       style_function=lambda x: {'color': 'red', 'fillOpacity': 0, 'weight': 4}).add_to(mapa)

    for year in year_user:
        print(f'\nPobieranie danych dla roku {year}')
        layer_name=f'gugik:SkorowidzNMT{year}'

        params_nmt={'service': 'WFS',
                      'version': '1.0.0', 
                      'request': 'GetFeature',
                      'typeName': layer_name,
                      'outputFormat': 'text/xml; subType=gml/3.1.1',
                      'bbox': f'{minx},{miny},{maxx},{maxy}'}

        #SPRAWDZENIE STANU SERWERA GUGIK (czasem nie dziala)
        try:
            response=requests.get(wfs_nmt, params=params_nmt, headers=headers, timeout=60)

            if response.status_code == 200:
                print(f'[TEST] Serwer odpowiedzial prawidlowo, rozmiar odp: {len(response.content)} bajtów.')
            else:
                print(f'[TEST] Serwer zwrocil kod bledu: {response.status_code}')

            root=ET.fromstring(response.content)
            features_data=[]

            for entry in root.iter():
                if 'SkorowidzNMT' in entry.tag:
                    data={}
                    for child in entry:
                        clean_tag=child.tag.split('}')[-1]
                        if child.text and child.text.strip():
                            data[clean_tag]=child.text

                    lc, uc=None, None
                    for sub in entry.iter():
                        if 'lowerCorner' in sub.tag: lc=sub.text.split()
                        elif 'upperCorner' in sub.tag: uc=sub.text.split()

                    if lc and uc:
                        data['geometry']=box(float(lc[0]), float(lc[1]), float(uc[0]), float(uc[1]))
                        features_data.append(data)

            if not features_data:
                print(f'Brak arkuszy NMT dla roku {year} w tym obszarze')
                continue

            skorowidze_kwadrat=gpd.GeoDataFrame(features_data, crs='EPSG:2180')
            skorowidze=gpd.sjoin(skorowidze_kwadrat, powiat_test, predicate='intersects')

            if skorowidze.empty:
                print(f'Po filtracji brak arkuszy dla roku {year}')
                continue

            if not skorowidze.empty:
                #---TUTAJ WAZNA ZMIANA, POBIERAM TEZ CRS---
                #---WYKLUCZENIE FORMATU ASCII TBD Z POBIERANIA/MOZAIKOWANIA---
                #TBD zostaje widoczne na mapie folium ale NIE trafia do listy
                #pobierania ani do dalszego przetwarzania
                tbd_pominiete=0
                pominieto_uklad=0
                ldp=[] #lista dalszego pobierania
                widziane_url=set()
                for _, row in skorowidze.iterrows():
                    format_arkusza=str(row.get('format', '')).strip().upper()

                    if 'TBD' in format_arkusza:
                        tbd_pominiete += 1
                        continue

                    uklad=str(row.get('uklad_xy', '')).strip()

                    #---BRAK ZGADYWANIA UKLADU---
                    #jak uklad wspolrzednych arkusza nie jest rozpoznany, plik jest
                    #pomijany (NIE trafia do pobierania/konwersji), zamiast domyslnie
                    #zakladac PL-1992 - zeby nie przetwarzac danych w zlym ukladzie
                    if uklad not in UKLADY:
                        pominieto_uklad += 1
                        print(f"[UWAGA] Arkusz {row.get('godlo', '?')} (zgloszenie {row.get('nr_zglosz', '?')}) "
                              f"ma nierozpoznany uklad wspolrzednych ('{uklad}') - POMINIETY.")
                        continue

                    epsg=UKLADY[uklad]

                    #ZMIANA: wpis niesie tez date aktualizacji, obrys, godlo, zgloszenie i format -
                    #po nich porownuje sie skorowidz z zawartoscia ZARR (aktualizacja przyrostowa).
                    #download_nmt_files() uzywa z wpisu tylko 'url', wiec dodatkowe klucze nie przeszkadzaja
                    if row['url_do_pobrania'] in widziane_url:
                        continue
                    widziane_url.add(row['url_do_pobrania'])
                    try:
                        akt_data_iso=pd.to_datetime(row['akt_data']).date().isoformat()
                    except Exception:
                        akt_data_iso=''

                    ldp.append({'url': row['url_do_pobrania'],
                                'epsg': epsg,
                                'plik': str(row['url_do_pobrania']).split('/')[-1],
                                'godlo': str(row.get('godlo', '')),
                                'nr_zglosz': str(row.get('nr_zglosz', '')),
                                'format': format_arkusza,
                                'akt_data': akt_data_iso,
                                'bounds': [float(v) for v in row['geometry'].bounds]})

                if tbd_pominiete:
                    print(f'Znaleziono i pominieto {tbd_pominiete} arkuszy w formacie ASCII TBD (pobieranie/mozaikowanie)')

                if pominieto_uklad:
                    print(f'Znaleziono i pominieto {pominieto_uklad} arkuszy z nierozpoznanym ukladem wspolrzednych')

                if ldp:
                    dane_do_pobrania[year]={'linki': ldp,
                                            'folder': os.path.join(cache_dir, f'nmt_{year}_{powiat_save}')}

            print(f'Znaleziono {len(skorowidze)} arkuszy dla roku {year}')

            skorowidze_4326=skorowidze.to_crs(epsg=4326)
            print(f'Lista kampanii pomiarowych w {year} (nr zgloszenia), ID i format:')
            info=skorowidze[['nr_zglosz', 'format']].drop_duplicates()
            for _, row in info.iterrows():
                print(f" - Zgloszenie: {row['nr_zglosz']} | Format: {row['format']}")

            if mapa is not None:
                skorowidze_4326=skorowidze.to_crs(epsg=4326)

                for col in skorowidze_4326.columns:
                    if col != 'geometry':
                        skorowidze_4326[col]=skorowidze_4326[col].apply(lambda x: str(x) if x is not None else '')

                nmt_kampania=skorowidze_4326['nr_zglosz'].unique()
                colormap=cm.linear.Paired_08.scale(0, max(2, len(nmt_kampania)))

                #---GRUPOWANIE WARSTW PO (ZGLOSZENIE, FORMAT)---
                #warstwy w roznych formatach sie na siebie nakladaja
                #grupuje po parze (nr_zglosz, format), nie tylko po zgloszeniu
                kombinacje=skorowidze_4326[['nr_zglosz', 'format']].drop_duplicates()

                for _, kombinacja in kombinacje.iterrows():
                    n=kombinacja['nr_zglosz']
                    fmt=kombinacja['format']

                    nr_kampanii=skorowidze_4326[
                        (skorowidze_4326['nr_zglosz'] == n) & (skorowidze_4326['format'] == fmt)]

                    i=list(nmt_kampania).index(n)
                    color_hex=colormap(i)

                    warstwa_zgloszenie=folium.FeatureGroup(name=f'[{year}] {fmt} - Zgloszenie {n}')

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

    if mapa is not None:
        folium.LayerControl(collapsed=False).add_to(mapa)

        nazwa_save=f'wynik_nmt_{powiat_save}_{lata_save}.html'
        save_dir=os.path.join(cache_dir, nazwa_save)

        mapa.save(save_dir)
        print(f'Mapa zapisana w: {save_dir}')
    else:
        print("Mapa HTML pominieta ('mapa': {'zapisz': false})")

    # ---POBIERANIE I PRZETWARZANIE---
    if not dane_do_pobrania:
        print('Brak danych do pobrania.\nZAKONCZONO')
    else:
        #---ZARR: JEDEN STORE NA POWIAT, JEDNA WARSTWA CZASOWA = JEDEN ROK---
        sciezka_zarr=os.path.join(cache_dir, f'{powiat_save}_WIELOCZASOWA.zarr')
        magazyn=Magazyn(sciezka_zarr, ZARR_FORMAT, BLOK_PX)
        ustawienia_zarr={'rozdzielczosc_bazowa': float(conf_cellsize) if conf_cellsize else None,
                         'poziomy': conf_poziomy, 'resampling_poziomow': conf_resampling,
                         'blok_px': BLOK_PX, 'zarr_format': ZARR_FORMAT}
        geom_2180=powiat_test.to_crs(epsg=2180).geometry.iloc[0] #wymuszam w razie czego 2180

        for year, info in dane_do_pobrania.items():
            liczba=len(info['linki'])

            #nowy podzial folderow
            main_dir_yr=os.path.join(cache_dir, f'nmt_{year}_{powiat_save}')

            dir_entry=os.path.join(main_dir_yr, 'dane_wejsciowe')       #pliki wejsciowe z geoportalu (tiff/asc/xyz)
            dir_2000=os.path.join(main_dir_yr, 'tiff_pl2000')           #tiffy w pl-2000
            dir_1992=os.path.join(main_dir_yr, 'tiff_pl1992')           #tiffy w pl-1992 (konwertowane i nie)

            for dirs in ([dir_entry, dir_2000, dir_1992] if conf_konwersja else [dir_entry]):
                os.makedirs(dirs, exist_ok=True)

            #ZMIANA: mapa HTML jest zapisywana TYLKO w folderze ogolnym (cache_dir), bez kopii w folderach lat

            #---PLAN: CO ZMIENILO SIE OD OSTATNIEJ AKTUALIZACJI (manifest zapisany w store'ze)---
            #plan porownuje skorowidz z zawartoscia ZARR - ma sens tylko gdy ZARR jest budowany;
            #przy 'konwersja': false albo trybie geotiff (zarr wylaczony) pobieranie/konwersja dzieje sie zawsze
            if conf_konwersja and conf_mosaic is not False:
                rekord=magazyn.manifest().get(int(year))
                plan=zaplanuj(rekord, info['linki'], wymus=conf_wymus)
            else:
                plan={'akcja': 'nowa', 'brudne': None, 'dodane': [], 'usuniete': [], 'zmienione': []}
            opisy_planu={'nowa': 'NOWA warstwa czasowa',
                         'zmiana': f"ZMIANA w skorowidzu (nowe/zmienione: {len(plan['dodane'])}, "
                                   f"usuniete: {len(plan['usuniete'])}) - przebuduje tylko dotkniete bloki",
                         'przebudowa': 'PRZEBUDOWA calej warstwy (poprzedni zapis przerwany albo wymuszona)',
                         'aktualna': 'AKTUALNA'}
            if conf_konwersja and conf_mosaic is not False:
                print(f'\n[ZARR] Rok {year}: {liczba} arkuszy w skorowidzu | {opisy_planu[plan["akcja"]]}')
            else:
                print(f'\nRok {year}: {liczba} arkuszy w skorowidzu')
            if plan['akcja'] == 'aktualna':
                print(f'Rok {year}: warstwa jest aktualna - nic do zrobienia.')
                continue

            #decyzja o zapisie do ZARR - z config.json (jesli podana) albo interaktywnie
            if not conf_konwersja:
                create_mosaic=False
            elif conf_mosaic is not None:
                create_mosaic=bool(conf_mosaic)
                print(f'Zapis do ZARR dla roku {year} ustawiony z config.json: '
                      f'{"TAK" if create_mosaic else "NIE"}')
            else:
                while True:
                    decyzja_moz=input(f'\nZapisac arkusze roku {year} do ZARR (warstwa czasowa)? (t/n): ').lower().strip()
                    if decyzja_moz in ['t', 'n']:
                        create_mosaic=(decyzja_moz=='t')
                        break
                    print(f'Wpisz [t] dla TAK lub [n] dla NIE.')

            #decyzja o pobieraniu - z config.json (jesli podana) albo interaktywnie
            if conf_download is not None:
                do_download=bool(conf_download)
                print(f'Pobieranie dla roku {year} ustawione z config.json: '
                      f'{"TAK" if do_download else "NIE"}')
            else:
                while True:
                    decyzja_downl=input(f'Pobrac {liczba} arkuszy dla roku {year}? (t/n): ').lower().strip()
                    if decyzja_downl in ['t', 'n']:
                        do_download=(decyzja_downl=='t')
                        break
                    print(f'Wpisz [t] dla TAK lub [n] dla NIE.')

            #WYKONANIE AKCJI
            folder_rok=info['folder']
            os.makedirs(folder_rok, exist_ok=True)

            #pobieranie tylko jesli uzytkownik chcial
            if do_download:
                print(f'\n---POBIERANIE DANYCH DLA ROKU {year}---')
                #arkusz wydany PONOWNIE (ta sama nazwa, inna data): stare archiwum musi zniknac,
                #inaczej download_nmt_files() uznaloby je za juz pobrane
                for nazwa_pliku in plan['zmienione']:
                    stary_plik=os.path.join(dir_entry, nazwa_pliku)
                    if os.path.exists(stary_plik):
                        os.remove(stary_plik)
                        print(f'Usunieto stare archiwum (arkusz wydany ponownie): {nazwa_pliku}')
                download_nmt_files(info['linki'], dir_entry, max_workers=WATKI_POBIERANIA, proby=PROBY_POBIERANIA,
                                   timeout=(TIMEOUT_POLACZENIA, TIMEOUT_ODCZYTU))

            if not conf_konwersja:
                print(f'Konwersja wylaczona - surowe pliki roku {year} sa w: {dir_entry}')
                continue

            print(f'---PRZETWARZANIE DANYCH DLA ROKU {year}---')

            #slowniki powiazan zeby przetransportowac CRSy i daty
            #ZMIANA: daty biore z wpisow TEGO roku (wczesniej z 'skorowidze', czyli z OSTATNIEGO roku petli)
            mapa_uklady={p['plik']: p['epsg'] for p in info['linki']}
            mapa_daty={p['plik']: pd.to_datetime(p['akt_data']) for p in info['linki'] if p['akt_data']}

            #KONWERSJA + REPROJEKCJA -> lista GeoTIFF-ow (EPSG:2180) z tagami daty i pliku zrodlowego
            tiffs_do_zarr=process_data(dir_entry, mapa_uklady, mapa_daty, extract=do_download,
                                       dir_a=dir_2000, dir_b=dir_1992)

            if create_mosaic:
                wynik=aktualizuj_rok_zarr(sciezka_zarr, geom_2180, int(year), info['linki'], tiffs_do_zarr,
                                          plan, ustawienia_zarr, nazwa_powiatu=powiat_save,
                                          folder_wejsciowy=dir_entry)
                print(f'ZARR, rok {year}: {wynik.get("akcja")}')

            if conf_geotiff:
                #kolejnosc najnowsze->najstarsze - jak przy zapisie do ZARR (find_date z nfp_mosaics.py)
                posortowane=sorted(tiffs_do_zarr, key=lambda p: find_date(p, mapa_daty), reverse=True)
                sciezka_mozaiki=os.path.join(cache_dir, 'mozaiki', f'{powiat_save}_{year}.tif')
                try:
                    _, uzyta_cs=zbuduj_mozaike_geotiff(posortowane, geom_2180, sciezka_mozaiki,
                                                       cellsize=float(conf_cellsize) if conf_cellsize else None)
                    print(f'GeoTIFF, rok {year}: mozaika ({uzyta_cs:g} m) zapisana w {sciezka_mozaiki}')
                except Exception as e:
                    print(f'[BLAD] GeoTIFF, rok {year}: nie udalo sie zbudowac mozaiki: {e}')

            if not create_mosaic and not conf_geotiff:
                print(f'Kafelki zapisano w folderze: {dir_1992}')

    print('\nZAKONCZONO')
except Exception as _e:
    print(f'\n[BLAD] {_e}')
    import traceback
    traceback.print_exc()
finally:
    input('\nNacisnij Enter, aby zakonczyc.')