import os
import glob
import numpy as np
import rasterio
from rasterio.merge import merge
from rasterio.mask import mask
from rasterio.warp import calculate_default_transform, reproject, Resampling
from rasterio.crs import CRS
from rasterio.transform import from_origin
from rasterio.vrt import WarpedVRT
from rasterio.enums import Resampling
from shapely.geometry import shape
from pathlib import Path
import time #zeby uniknac przedwczesnego dzialania programu i bledu
import re #https://docs.python.org/3/library/re.html
import shutil
import traceback
import zipfile
from downloader import unzip
import pandas as pd


def detect_crs_from_xyz(file_path):
    #---ANALIZA PIERWSZEJ LINII PLIKU ABY AUTOMATYCZNIE WYKRYC CRS---

    try:
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            #przeszukuje pierwsze 200 linii
            for _ in range(200):
                line = f.readline()
                if not line:
                    break
                
                line_str = line.strip()
                if not line_str:
                    continue
                
                #jezeli linia zaczyna się od liter to pomijam
                if re.match(r'^[a-zA-Z#]', line_str):
                    continue
                
                #dziele na czesci, to musi zostac bo to niekoniecznie bialy znak
                parts = re.split(r'[\s,;]+', line_str)
                
                #wyrzucam puste elementy
                parts = [p for p in parts if p]
                
                if len(parts) >= 2:
                    try:
                        val1 = float(parts[0])
                        val2 = float(parts[1])
                        
                        #to na w razie czego, wspolrzedne na pewno sa wieksze niz to!!!!!!!!
                        if val1 < 100000 and len(parts) >= 3:
                            # Jeśli pierwszy to ID, sprawdzamy kolumnę 2 i 3 jako potencjalne X i Y
                            val1 = float(parts[1])
                            val2 = float(parts[2])
                        
                        if val1 < 100000 or val2 < 100000:
                            continue
                            
                        #---UKLAD WSPOLRZEDNYCH---
                        #wspolrzedna PL-2000 ma 7 cyfr
                        if val1 > 4000000 or val2 > 4000000:
                            wsp_strefy = val1 if val1 > 4000000 else val2
                            strefa = int(str(int(wsp_strefy))[0])
                            
                            if strefa == 5: return CRS.from_epsg(2176)
                            if strefa == 6: return CRS.from_epsg(2177)
                            if strefa == 7: return CRS.from_epsg(2178)
                            if strefa == 8: return CRS.from_epsg(2179)
                        
                        #wspolrzedna PL-1992 ma 6 cyfr
                        if (100000 < val1 < 1000000) and (100000 < val2 < 1000000):
                            return CRS.from_epsg(2180)
                            
                    except ValueError:
                        continue
                        
    except Exception as e:
        print(f"[UWAGA] Blad podczas analizy pliku {os.path.basename(file_path)}: {e}")

    #NIE zgaduje ukladu na sile, jesli nie da sie jednoznacznie okreslic
    #plik jest pomijany (nie wchodzi do dalszego przetwarzania)
    print(f"[UWAGA] Nie mozna zidentyfikowac ukladu wspolrzednych (CRS) pliku "
          f"{os.path.basename(file_path)} - plik zostanie POMINIETY w przetwarzaniu.")
    return None

from AIAG2GT import AIAG2GTIFF
from ASCII2GT import ASCII2GT
from nfp_mosaics import generate_nfp_mosaics, wyrownanie_do_siatki

def _zapisz_date_w_tagach(sciezka_tif, mapa_daty, nazwa_pliku, mapowanie_zrodlowy_zip=None):
    if not mapa_daty:
        return
    data = mapa_daty.get(nazwa_pliku)
    if data is None and mapowanie_zrodlowy_zip:
        nazwa_zip=mapowanie_zrodlowy_zip.get(nazwa_pliku)
        if nazwa_zip:
            data = mapa_daty.get(nazwa_zip)
    if data is None:
        print(f'[PROCES] UWAGA: brak daty dla "{nazwa_pliku}".')
        return
    try:
        with rasterio.open(sciezka_tif, 'r+') as dst:
            dst.update_tags(akt_data=data.isoformat())
    except Exception as e:
        print(f'[PROCES] UWAGA: nie udalo sie zapisac daty w tagach {sciezka_tif}: {e}')


#procesowanie danych z przekazaniem decyzji tak/nie
def process_data(zip_dir, final_output_path, geometry, mapa_ukladow=None,
                 mapa_daty=None, create_mosaic=True, extract=True,
                 dir_a=None, dir_b=None, target_cellsize=None,
                 return_kafle_info=False, metoda_nfp='nearest',
                 tylko_test_pokrycia=False):
    '''
    tylko_test_pokrycia : jesli True, pomija budowe bazy
    i zapis mozaik eksperymentalnych wewnatrz generate_nfp_mosaics
    wywolanie sluzy tylko do wyznaczenia listy uzyte_kafle
    '''
    _MAPA_METOD_NFP = {'nearest': Resampling.nearest,
                       'bilinear': Resampling.bilinear,
                       'bicubic': Resampling.cubic, 'cubic': Resampling.cubic}
    metoda_nfp_key = str(metoda_nfp).strip().lower()
    resampling_nfp = _MAPA_METOD_NFP[metoda_nfp_key]

    uzyte_kafle = None
    baza_natywna = None

    if mapa_ukladow is None:
        mapa_ukladow = {}

    if dir_a is None:
        dir_a = zip_dir
    if dir_b is None:
        dir_b = zip_dir
    os.makedirs(dir_a, exist_ok=True)
    os.makedirs(dir_b, exist_ok=True)

    # ---1. ROZPAKOWANIE WSZYSTKICH ZIP ---
    #---MAPOWANIE nazwa_pliku_wewnatrz_archiwum -> nazwa_archiwum_zrodlowego---
    #budowane TERAZ (przed/przy rozpakowywaniu), zeby _zapisz_date_w_tagach()
    #mialo jak dopasowac date, gdy nazwa wewnetrzna != nazwa zipa (patrz
    #komentarz w _zapisz_date_w_tagach powyzej).
    mapowanie_zrodlowy_zip={}
    if extract:
        zip_files = glob.glob(os.path.join(zip_dir, '*.zip'))
        for z_path in zip_files:
            print (f'[PROCES] Rozpakowywanie pliku {os.path.basename(z_path)}')

            #---ODCZYT LISTY PLIKOW W ARCHIWUM (przed rozpakowaniem)---
            #niezalezne od tego, jak dokladnie unzip() organizuje wynik na
            #dysku (podfoldery itp.) - identyfikujemy pliki po samej nazwie
            #(os.path.basename), tak samo jak pozniej robi to rglob() w
            #kroku 2 ponizej.
            try:
                with zipfile.ZipFile(z_path, 'r') as zf:
                    nazwy_wewnatrz=[os.path.basename(n) for n in zf.namelist() if n and not n.endswith('/')]
                for nazwa_wew in nazwy_wewnatrz:
                    mapowanie_zrodlowy_zip[nazwa_wew]=os.path.basename(z_path)
            except Exception as e:
                print(f'[PROCES] UWAGA: nie udalo sie odczytac listy plikow z {os.path.basename(z_path)}: '
                      f'({e})')

            #---ZABEZPIECZENIE: JEDEN USZKODZONY ZIP NIE PRZERYWA CALEGO PRZEBIEGU---
            #(np. uszkodzone/niedokonczone pobranie, albo serwer zwrocil strone
            #bledu zamiast archiwum) - plik jest pomijany z czytelnym ostrzezeniem
            #zamiast wywalac cala funkcje (i caly powiat/rok) wyjatkiem BadZipFile.
            try:
                unzip(z_path, zip_dir)
            except Exception as e:
                print(f'[PROCES] BLAD: {os.path.basename(z_path)} jest uszkodzony: ({e}).'
                      f'Pominieto plik. Usun plik i rozpocznij pobieranie ponownie.')
                continue
    
    #!!!WAZNE!!! daje czas na pelne wczytanie danych na dysk
    time.sleep(2.0)
    print(f'\n[PROCES] Rozpakowano pliki zrodlowe')

    #---2. SZUKANIE DOSTEPNYCH FORMATOW NMT---
    tiffs_to_mosaic=[]
    
    wyszukane_pliki = []
    widziane_sciezki = set()

    folder_path=Path(zip_dir)
    for ext in ['*.asc', '*.xyz', '*.txt']:
        for p in folder_path.rglob(ext):
            sciezka_norm = str(p.resolve()).lower()
            if sciezka_norm not in widziane_sciezki:
                widziane_sciezki.add(sciezka_norm)
                wyszukane_pliki.append(p)

    wyszukane_pliki = [str(p) for p in wyszukane_pliki]

    if not wyszukane_pliki:
        print(f"\n[PROCES] Nie znaleziono NMT (.xyz, .asc, .txt) po rozpakowaniu")
        return

    print(f"\n[PROCES] Wykryto {len(wyszukane_pliki)} plikow")
    
    #---3. KONWERSJA---
    for rfile in wyszukane_pliki:
        nazwa_pliku = os.path.basename(rfile)
        stem_pliku = nazwa_pliku.rsplit('.', 1)[0]

        kod_epsg=mapa_ukladow.get(nazwa_pliku, 2180)
        wykryty_crs = CRS.from_epsg(kod_epsg)

        if kod_epsg == 2180:
            tif_final_path = os.path.join(dir_b, stem_pliku + '.tif')
        else:
            tif_final_path = os.path.join(dir_a, stem_pliku + '.tif')

        print(f"[PROCES] -> Plik: {nazwa_pliku} | EPSG:{kod_epsg}")

        new_tiffs=[]

        if os.path.exists(tif_final_path):
            print(f"[PROCES] Pominieto konwersje (plik juz istnieje): {os.path.basename(tif_final_path)}")
            new_tiffs.append(tif_final_path)
        else:
            try:
                with open(rfile, 'r', encoding='utf-8') as f:
                    first_line = f.readline().lower()

                if 'ncols' in first_line:
                    AIAG2GTIFF(rfile, tif_final_path, epsg_code=kod_epsg)
                else:
                    ASCII2GT(rfile, tif_final_path, epsg_code=kod_epsg)

                if os.path.exists(tif_final_path):
                    new_tiffs.append(tif_final_path)

            except Exception as e:
                print(f"[PROCES] BLAD! Konwersja pliku {nazwa_pliku} sie nie powiodla: {e}")
                continue

        #---4. REPROJEKCJA DO 1992 W PRZYPADKU PL-2000---
        path_to_add = tif_final_path
        reproject_ok=True

        if os.path.exists(tif_final_path):
            if kod_epsg != 2180:
                tif_reprojected_path = os.path.join(dir_b, stem_pliku + '_2180r.tif')
                dst_crs = CRS.from_epsg(2180)

                #---UWAGA: brak walidacji integralnosci (patrz komentarz wyzej)---
                #jesli po tym kroku pojawi sie blad GDAL przy odczycie tego
                #pliku pozniej w pipeline, usun go recznie i uruchom ponownie.
                if os.path.exists(tif_reprojected_path):
                    print(f'[PROCES] Pominieto reprojekcje (plik juz istnieje): '
                          f'{os.path.basename(tif_reprojected_path)}')
                    path_to_add = tif_reprojected_path
                    reproject_ok = True
                    _zapisz_date_w_tagach(path_to_add, mapa_daty, nazwa_pliku, mapowanie_zrodlowy_zip)
                    tiffs_to_mosaic.append(path_to_add)
                    continue

                try:
                    with rasterio.open(tif_final_path) as src:
                        tmp_transform, tmp_width, tmp_height=\
                            calculate_default_transform(wykryty_crs, dst_crs,
                                                        src.width, src.height,
                                                        *src.bounds)
                        tmp_left=tmp_transform.c
                        tmp_top=tmp_transform.f
                        tmp_right=tmp_left + tmp_width * tmp_transform.a
                        tmp_bottom=tmp_top + tmp_height * tmp_transform.e  #e jest ujemne

                        cellsize=round(abs(src.transform.a), 2)
                        left, bottom, right, top=wyrownanie_do_siatki(
                            (tmp_left, tmp_bottom, tmp_right, tmp_top), cellsize)
                        dst_width=max(1, int(round((right - left) / cellsize)))
                        dst_height=max(1, int(round((top - bottom) / cellsize)))
                        dst_transform=from_origin(left, top, cellsize, cellsize)

                        meta_rep = src.meta.copy()
                        meta_rep.update({'crs': dst_crs,
                            'transform': dst_transform,
                            'width': dst_width,
                            'height': dst_height})
                        
                        with rasterio.open(tif_reprojected_path, 'w', **meta_rep) as dst:
                            destination_array = np.ones((dst_height, dst_width), dtype=np.float32) * src.nodata
                            reproject(source=rasterio.band(src, 1), destination=destination_array,
                                      src_transform=src.transform, src_crs=wykryty_crs,
                                      dst_transform=dst_transform, dst_crs=dst_crs,
                                      resampling=Resampling.nearest)
                            if src.nodata is not None and np.isnan(src.nodata):
                                destination_array = np.round(destination_array, 2)
                            else:
                                maska = destination_array != src.nodata
                                destination_array[maska] = np.round(destination_array[maska], 2)

                            dst.write(destination_array, 1)
                    
                    print(f'[PROCES] Plik PL-2000 zachowany w: {tif_final_path}')

                    path_to_add = tif_reprojected_path
                    reproject_ok=True
                    print(f'[PROCES] Reprojekcja zakonczona: {os.path.basename(tif_reprojected_path)}')
                    
                except Exception as e:
                    reproject_ok=False
                    print(f"[PROCES] BLAD: Reprojekcja do PL-1992 nie powiodla sie: {e}")
                    continue

            if reproject_ok:
                _zapisz_date_w_tagach(path_to_add, mapa_daty, nazwa_pliku, mapowanie_zrodlowy_zip)
                tiffs_to_mosaic.append(path_to_add)
            else:
                print(f'[PROCES] Plik {nazwa_pliku} nie zostal uwzgledniony w mozaice.')

    #FOLDER WYNIKOWY
    output_folder = os.path.join(os.path.dirname(final_output_path))
    os.makedirs(output_folder, exist_ok=True)

    # ---5. PROSTOWANIE MACIERZY I REPROJEKCJI VRT/OPCJONALNE MOZAIKOWANIE---
    if not create_mosaic:
        print(f'[PROCES] {len(tiffs_to_mosaic)} plikow zapisano w: {output_folder}')
        wynik_do_zwrotu = None

    else:
        print('\n[PROCES] Przetwarzanie rastrow do mozaik NFP.')

        base_name = Path(final_output_path).stem

        try:
            if tylko_test_pokrycia:
                #---SZYBKA SCIEZKA: TYLKO WYZNACZENIE uzyte_kafle---
                #metody={} + return_baza_natywna=False -> generate_nfp_mosaics
                #pomija zetap 1 (merge blokami calej bazy)
                #i etap 2 (resampling do target_cellsize)
                
                wyniki_nfp, uzyte_kafle = generate_nfp_mosaics(
                    tiffs_to_mosaic=tiffs_to_mosaic,
                    geometry=geometry,
                    mapa_daty=mapa_daty,
                    output_dir=output_folder,
                    base_name=base_name,
                    target_cellsize=target_cellsize,
                    metody={},
                    return_uzyte_kafle=True,
                    return_baza_natywna=False,
                )
                baza_natywna = None
            else:
                wyniki_nfp, uzyte_kafle, baza_natywna = generate_nfp_mosaics(
                    tiffs_to_mosaic=tiffs_to_mosaic,
                    geometry=geometry,
                    mapa_daty=mapa_daty,
                    output_dir=output_folder,
                    base_name=base_name,
                    target_cellsize=target_cellsize,
                    metody={metoda_nfp_key: resampling_nfp, 'nearest': Resampling.nearest},
                    metoda_ujednolicania=resampling_nfp,
                    return_uzyte_kafle=True,
                    return_baza_natywna=True,
                )
        except Exception as e:
            #JESLI TU WYSTAPI BLAD GDAL W STYLU "TIFFReadDirectory: Failed to
            #read directory at offset ..." - to znak, ze jeden z plikow .tif
            #w dir_a/dir_b (tiff_pl2000/tiff_pl1992) jest uszkodzony (przerwany
            #wczesniej zapis). Trzeba go usunac z dysku i sprobowac ponownie
            print(f'[PROCES] Blad podczas mozaikowania NFP: {e}')
            traceback.print_exc()
            wyniki_nfp = None
            uzyte_kafle = None
            baza_natywna = None

        wynik_do_zwrotu = wyniki_nfp

    try:
        if dir_a != zip_dir and os.path.isdir(dir_a) and not os.listdir(dir_a):
            os.rmdir(dir_a)
            print(f'[PROCES] Usunieto pusty folder: {dir_a}')
    except Exception as e:
        print(f'[PROCES] Nie udalo sie usunac folderu {dir_a}: {e}')

    if return_kafle_info:
        return wynik_do_zwrotu, uzyte_kafle, baza_natywna
    return wynik_do_zwrotu