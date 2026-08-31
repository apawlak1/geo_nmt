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
    from fp_filters import fpdems, wczytaj_nmt, zapisz_nmt  #moj plik .py
    from pathlib import Path

    #---WCZYTANIE SCIEZKI CACHE Z CONFIG (wspolna funkcja z downloader.py, patrz tez wersja_dev.py)---
    #szukam w tym samym folderze co skrypty (bezpieczniej)
    script_dir=Path(__file__).resolve().parent
    config_path=script_dir / 'config.json'

    config_data=read_config(config_path, wymagane_klucze=('cache_dir',))
    cache_dir=config_data['cache_dir']
    os.makedirs(cache_dir, exist_ok=True)

    #---config'folium_v3' (powiat, rok/lata/, rozdzielczosc)---
    #jesli klucza brak/jest pusty skrypt pyta o input())
    #config obsluguje skrypt automatycznie lub recznie
    folium_config=config_data.get('folium_v3', {})
    conf_powiat=folium_config.get('powiat')
    conf_yr=folium_config.get('lata')
    conf_cellsize=folium_config.get('rozdzielczosc')  #target_cellsize dla mozaiki NFP, None=auto
    conf_mosaic=folium_config.get('mozaika')      #True/False -> pomija pytanie t/n, None -> pyta jak dawniej
    conf_download=folium_config.get('pobierz')      #True/False -> pomija pytanie t/n, None -> pyta jak dawniej

    #---GENERALIZACJA: "NFP" lub "FP", wybor uzytkownika (config lub interaktywnie)---
    #NFP  -> produktem koncowym jest mozaika NFP (metoda_nfp ponizej) - bez
    #         dalszej filtracji.
    #FP   -> na TEJ SAMEJ mozaice NFP dodatkowo stosowana jest metoda
    #         feature-preserving FPDEMS (fp_filters.fpdems) - to jest
    #         produkt koncowy.
    _DOZWOLONE_GENERALIZACJE={'NFP', 'FP'}
    conf_generalizacja=folium_config.get('generalizacja')
    if conf_generalizacja and str(conf_generalizacja).strip().upper() in _DOZWOLONE_GENERALIZACJE:
        generalizacja=str(conf_generalizacja).strip().upper()
        print(f'Generalizacja ustawiona z config.json: {generalizacja}')
    else:
        while True:
            generalizacja=input('\nBrak/nieprawidlowa "generalizacja" w config.json. '
                                'Wybierz generalizacje - NFP (sama mozaika) '
                                'czy FP (mozaika + feature-preserving) [NFP/FP]: ').strip().upper()
            if generalizacja in _DOZWOLONE_GENERALIZACJE:
                break
            print('Wpisz "NFP" albo "FP".')

    #---METODA NFP: ktorej metody resamplingu uzyc do budowy KONCOWEJ mozaiki---
    #(oraz, jesli uzyte kafle maja rozne rozdzielczosci miedzy soba, do ich
    #wstepnego ujednolicenia - patrz nfp_mosaics._ujednolic_rozdzielczosc).
    #Domyslnie 'bilinear' - jesli w analizie bledow (wersja_dev.py,
    #RAPORT.txt) 'bicubic' wypadl lepiej, ustaw tutaj 'bicubic'.
    conf_metoda_nfp=str(folium_config.get('metoda_nfp', 'bilinear')).strip().lower()
    if conf_metoda_nfp not in ('nearest', 'bilinear', 'bicubic', 'cubic'):
        print(f"UWAGA: nieznana 'metoda_nfp'='{conf_metoda_nfp}' w config.json - uzywam 'bilinear'.")
        conf_metoda_nfp='bilinear'
    print(f'Metoda NFP: {conf_metoda_nfp} | Generalizacja: {generalizacja}')

    #---SEKCJA 'fpdems' W config.json (parametry generalizacji koncowej)---
    #stosowane na koncu, PO zbudowaniu mozaiki NFP (metoda_nfp) - patrz sekcja
    #'POBIERANIE I PRZETWARZANIE' nizej, TYLKO gdy generalizacja=="FP".
    #Metoda FP jest wybrana na sztywno (fpdems z fp_filters.py), tu mozna
    #tylko dostroic jej parametry.
    fpdems_config=config_data.get('fpdems', {})


    #---SCIEZKI---
    wfs_nmtKR='https://mapy.geoportal.gov.pl/wss/service/PZGIK/NumerycznyModelTerenuKRON86/WFS/Skorowidze'
    wfs_nmt='https://mapy.geoportal.gov.pl/wss/service/PZGIK/NumerycznyModelTerenuEVRF2007/WFS/Skorowidze'

    headers={'User-Agent': 'Mozilla/5.0'}

    dane_do_pobrania={}

    #---GRANICE POWIATOW: WSPOLNA FUNKCJA Z downloader.py (identyczna z tym, czego uzywa wersja_dev.py)---
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
    #config.json ma "lata": ["2020"] - to w Pythonie LISTA ['2020'], nie string.
    #str(['2020']) daje TEKST "['2020']" (z nawiasami i cudzyslowem!), a jego
    #.split(',') (brak przecinka w srodku) zwracal JEDEN, zle sformatowany
    #"rok": "['2020']" - dokladnie to trafialo pozniej do zapytania WFS jako
    #SkorowidzNMT['2020'] (zamiast SkorowidzNMT2020), wiec serwer nie znajdowal
    #zadnej takiej warstwy i zwracal pusty wynik, mimo ze dane na serwerze
    #istnialy. Teraz obsluzone poprawnie oba przypadki: lista (z JSON) ORAZ
    #zwykly string "2020" lub "2020,2021" (gdyby ktos tak wpisal recznie).
    if isinstance(conf_yr, (list, tuple)):
        year_user=[str(l).strip() for l in conf_yr if str(l).strip()]
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

    powiat_4326=powiat_test.to_crs(epsg=4326)
    powiaty_4326=powiaty.to_crs(epsg=4326)

    for df in [powiat_4326, powiaty_4326]:
        for col in df.columns:
            if col != 'geometry':
                df[col]=df[col].apply(lambda x: str(x) if x is not None else '')

    print(f'\nTworzenie mapy HTML')
    mapa=folium.Map(location=[c_lat, c_lon], zoom_start=11, tiles='CartoDB positron')

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

                    ldp.append({'url': row['url_do_pobrania'],
                                'epsg': epsg})

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

    folium.LayerControl(collapsed=False).add_to(mapa)

    nazwa_save=f'wynik_nmt_{powiat_save}_{lata_save}.html'
    save_dir=os.path.join(cache_dir, nazwa_save)

    mapa.save(save_dir)
    print(f'Mapa zapisana w: {save_dir}')

    # ---POBIERANIE I PRZETWARZANIE---
    if not dane_do_pobrania:
        print('Brak danych do pobrania.\nZAKONCZONO')
    else:
        for year, info in dane_do_pobrania.items():
            liczba=len(info['linki'])

            #nowy podzial folderow
            main_dir_yr=os.path.join(cache_dir, f'nmt_{year}_{powiat_save}')

            dir_entry=os.path.join(main_dir_yr, 'dane_wejsciowe')       #pliki wejsciowe z geoportalu (tiff/asc/xyz)
            dir_2000=os.path.join(main_dir_yr, 'tiff_pl2000')           #tiffy w pl-2000
            dir_1992=os.path.join(main_dir_yr, 'tiff_pl1992')           #tiffy w pl-1992 (konwertowane i nie)

            for dirs in [dir_entry, dir_2000, dir_1992]:
                os.makedirs(dirs, exist_ok=True)

            #---ZAPIS KOPII MAPY HTML W FOLDERZE POWIAT_ROK (main_dir_yr)---
            mapa_w_folderze_roku=os.path.join(main_dir_yr, nazwa_save)
            mapa.save(mapa_w_folderze_roku)
            print(f'Mapa zapisana w: {mapa_w_folderze_roku}')

            #decyzja o mozaice - z config.json (jesli podana) albo interaktywnie
            if conf_mosaic is not None:
                create_mosaic=bool(conf_mosaic)
                print(f'Mozaikowanie dla roku {year} ustawione z config.json: '
                      f'{"TAK" if create_mosaic else "NIE"}')
            else:
                while True:
                    decyzja_moz=input(f'\nPolaczyc arkusze w mozaike dla roku {year}? (t/n): ').lower().strip()
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
                download_nmt_files(info['linki'], dir_entry)

            #if len(pliki_na_dysku) > 0:
                print(f'---PRZETWARZANIE DANYCH DLA ROKU {year}---')
                geom_2180=powiat_test.to_crs(epsg=2180).geometry.iloc[0] #wymuszam w razie czego 2180
                nazwa_pliku=f'NMT_{powiat_save}_{year}_FINAL.tif' 
                pelna_sciezka_wyniku=os.path.join(folder_rok, nazwa_pliku)

                #slownik powiazan zeby przetransportowac te CRSy
                mapa_uklady={}
                for p in info['linki']:
                    nazwa_pliku=p['url'].split('/')[-1]
                    mapa_uklady[nazwa_pliku]=p['epsg']

                #to samo dla akt_data
                mapa_daty={row['url_do_pobrania'].split('/')[-1]:
                           pd.to_datetime(row['akt_data'])
                           for index, row in skorowidze.iterrows()}


                #WYWOLANIE FUNKCJI Z DECYZJA O MOZAICE
                #dir_2000/dir_1992 przekazane jawnie, zeby pliki trafialy do wlasciwych podfolderow
                #a plik PL-2000 byl kasowany od razu po reprojekcji do PL-1992
                #target_cellsize=conf_cellsize: None -> auto (najgorsza/najwieksza
                #rozdzielczosc wsrod kafli, jak dotychczas), liczba -> wymuszona rozdzielczosc
                #docelowa mozaiki, ustawiona w sekcji folium_v3 pliku config.json
                #---SLOWNIK {nazwa_metody_nfp: sciezka_do_pliku} lub None gdy create_mosaic=False---
                nfp_result=process_data(dir_entry, pelna_sciezka_wyniku, geom_2180,
                                        mapa_uklady, mapa_daty,
                                        create_mosaic=create_mosaic, extract=do_download,
                                        dir_a=dir_2000, dir_b=dir_1992,
                                        target_cellsize=conf_cellsize,
                                        metoda_nfp=conf_metoda_nfp)

                #informacja w zaleznosci od trybu
                #---create_mosaic=True TERAZ DAJE KILKA PLIKOW (jedna mozaika na jedna metode nfp), WAZNE, TEZ DO DOPRACOWANIA---
                if create_mosaic:
                    if nfp_result:
                        print(f'Mozaiki resamplowane zapisane dla roku {year}:')
                        for nazwa_metody, sciezka in nfp_result.items():
                            print(f' - {nazwa_metody}: {sciezka}')

                        #---GENERALIZACJA KONCOWA: METODA FP "NA SZTYWNO" (FPDEMS)---
                        #Mozaika NFP (bilinear) sama w sobie NIE chroni krawedzi
                        #terenowych (doliny, grzbiety, uskoki) - jest to tylko
                        #resampling. Dlatego produktem koncowym dla uzytkownika
                        #jest ta sama mozaika, dodatkowo wygladzona metoda FPDEMS
                        #(fp_filters.fpdems), wybrana na sztywno na podstawie
                        #analizy bledow (patrz analiza_fp_generalizacji w
                        #analiza.py, wywolywanej z wersja_dev.py).
                        #---NFP I FP JAKO 2 NIEZALEZNE PRODUKTY (NIE LANCUCH)---
                        #Obie galezie wychodza z TEJ SAMEJ bazy natywnej (Etap 1
                        #w nfp_mosaics.py, wspolny/liczony raz) i sa dalej
                        #NIEZALEZNE od siebie (Etap 2 wykonany osobno dla kazdej
                        #metody, patrz process_data() w processor.py):
                        #  NFP -> baza natywna resamplowana metoda_nfp (bilinear/
                        #         bicubic) - to jest kompletny produkt NFP sam w sobie.
                        #  FP  -> baza natywna resamplowana metoda 'nearest' (BEZ
                        #         zadnej interpolacji/zamazania krawedzi), a
                        #         DOPIERO na tym surowym wyniku stosowany jest
                        #         fpdems. FP NIE powstaje z mozaiki NFP - gdyby
                        #         tak bylo, fpdems "chronilby krawedzie", ktore
                        #         interpolacja NFP juz wczesniej zamazala.
                        #Dokladnie tak, jak metody FP byly walidowane w analizie
                        #bledow (wersja_dev.py: analiza_fp_generalizacji zawsze
                        #dostawala mozaike 'nearest', nigdy 'bilinear').
                        if generalizacja == 'NFP':
                            sciezka_nfp=nfp_result.get(conf_metoda_nfp)
                            if sciezka_nfp:
                                print(f'\nProdukt koncowy (NFP, {conf_metoda_nfp}) '
                                      f'dla roku {year}: {sciezka_nfp}')
                            else:
                                print(f'BLAD: brak mozaiki NFP ({conf_metoda_nfp}) dla roku {year}.')

                        elif generalizacja == 'FP':
                            sciezka_nearest=nfp_result.get('nearest')
                            if sciezka_nearest:
                                print(f'\nFiltracja Feature-Preserving (FPDEMS) na bazie '
                                      f'natywnej (nearest) dla roku {year}...')
                                try:
                                    dem_nearest, cellsize_nearest, profil_fp=wczytaj_nmt(sciezka_nearest)

                                    dem_fp=fpdems(
                                        dem_nearest, cellsize_nearest,
                                        theta_t_deg=fpdems_config.get('theta_t_deg', 15.0),
                                        iteracje_normalnych=fpdems_config.get('iteracje_normalnych', 5),
                                        max_diff=fpdems_config.get('max_diff', 0.5))

                                    sciezka_fp=sciezka_nearest.replace(
                                        '_NFP_nearest.tif', '_FINAL_fpdems.tif')
                                    zapisz_nmt(dem_fp, profil_fp, sciezka_fp)

                                    print(f'Ostateczny NMT (generalizacja FP) zapisany w: {sciezka_fp}')
                                except Exception as e:
                                    print(f'BLAD podczas generalizacji FP dla roku {year}: {e}')
                            else:
                                print(f'BLAD: brak mozaiki "nearest" (baza dla FP) dla roku {year}.')
                    else:
                        print(f'Nie udalo sie zbudowac mozaiki dla roku {year}.')
                else:
                    print(f'Kafelki zapisano w folderze: {os.path.join(os.path.dirname(pelna_sciezka_wyniku), "wyniki_konwersji")}')

    print('\nZAKONCZONO')
except Exception as _e:
    print(f'\n[BLAD] {_e}')
    import traceback
    traceback.print_exc()
finally:
    input('\nNacisnij Enter, aby zakonczyc.')