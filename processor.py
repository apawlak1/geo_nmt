import os
import zipfile
import glob
import numpy as np
import rasterio
from rasterio.merge import merge
from rasterio.mask import mask
from rasterio.warp import calculate_default_transform, reproject, Resampling
from rasterio.crs import CRS
from rasterio.vrt import WarpedVRT
from rasterio.enums import Resampling
from shapely.geometry import shape
from pathlib import Path
import time #zeby uniknac przedwczesnego dzialania programu i bledu
import re #https://docs.python.org/3/library/re.html
import shutil
from unzip import unzip
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


try:
    from unzip import unzip
except ImportError:
    def unzip(zip_p, extract_p):
        with zipfile.ZipFile(zip_p, 'r') as zip_ref:
            zip_ref.extractall(extract_p)

from AIAG2GT import AIAG2GTIFF
from ASCII2GT import ASCII2GT

#procesowanie danych z przekazaniem decyzji tak/nie
def process_data(zip_dir, final_output_path, geometry, mapa_ukladow=None,
                 mapa_daty=None, create_mosaic=True, extract=True,
                 dir_a=None, dir_b=None):
    if mapa_ukladow is None:
        mapa_ukladow = {}

    #---FOLDERY POSREDNIE (PL-2000) I DOCELOWE (PL-1992)---
    #dir_a = tiff_pl2000 (pliki PRZED reprojekcja, kasowane od razu po niej)
    #dir_b = tiff_pl1992 (pliki PO konwersji/reprojekcji, zostaja na dysku)
    #jesli nie podano (stare wywolania), zachowuje sie jak dawniej - wszystko w zip_dir
    if dir_a is None:
        dir_a = zip_dir
    if dir_b is None:
        dir_b = zip_dir
    os.makedirs(dir_a, exist_ok=True)
    os.makedirs(dir_b, exist_ok=True)

    # ---1. ROZPAKOWANIE WSZYSTKICH ZIP ---
    if extract:
        zip_files = glob.glob(os.path.join(zip_dir, '*.zip'))
        for z_path in zip_files:
            print (f'[PROCES] Rozpakowywanie pliku {os.path.basename(z_path)}')
            unzip(z_path, zip_dir)
    
    #!!!WAZNE!!! daje czas na pelne wczytanie danych na dysk
    time.sleep(2.0)
    print(f'\n[PROCES] Rozpakowano pliki zrodlowe')

    #---2. SZUKANIE DOSTEPNYCH FORMATOW NMT---
    tiffs_to_mosaic=[]
    
    #szukam mozliwych rozszerzen
    #system plikow jest case-insensitive, wiec OBA wzorce dopasowywaly TEN SAM plik (podwojenie)
    #szukam teraz kazdego rozszerzenia TYLKO RAZ (malymi literami)
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

        #---ZAPISYWANIE TIFF DO FOLDERÓW---
        #jesli plik jest juz w PL-1992 (2180), konwersja od razu ladowana do folderu docelowego (dir_b)
        #jesli plik jest w PL-2000 (2176-2179), najpierw ladowany do folderu tymczasowego (dir_a),
        #a po udanej reprojekcji do 2180 - kasowany (patrz nizej)
        if kod_epsg == 2180:
            tif_final_path = os.path.join(dir_b, stem_pliku + '.tif')
        else:
            tif_final_path = os.path.join(dir_a, stem_pliku + '.tif')

        print(f"[PROCES] -> Plik: {nazwa_pliku} | EPSG:{kod_epsg}")

        #---URUCHAMIANIE KONWERTEROW Z PRZEKAZANIEM CRS---
        new_tiffs=[]

        try:
            with open(rfile, 'r', encoding='utf-8') as f:
                #czytam pierwsza linie dla spr pliku (grid czy inny) lub format
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
        reproject_ok=True   #wartosc startowa

        if os.path.exists(tif_final_path):
            if kod_epsg != 2180:
                #plik docelowy PO reprojekcji zapisuje juz w folderze tiff_pl1992 (dir_b)
                tif_reprojected_path = os.path.join(dir_b, stem_pliku + '_2180r.tif')
                dst_crs = CRS.from_epsg(2180)
                
                try:
                    with rasterio.open(tif_final_path) as src:
                        #przeliczenie transformacji i wymiarow siatki
                        dst_transform, dst_width, dst_height =\
                            calculate_default_transform(wykryty_crs, dst_crs,
                                                        src.width, src.height,
                                                        *src.bounds)
                        meta_rep = src.meta.copy()
                        meta_rep.update({'crs': dst_crs,
                            'transform': dst_transform,
                            'width': dst_width,
                            'height': dst_height})
                        
                        #przeliczam i zapisuje piksele w nowej geometrii 2180
                        with rasterio.open(tif_reprojected_path, 'w', **meta_rep) as dst:
                            destination_array = np.ones((dst_height, dst_width), dtype=np.float32) * src.nodata
                            reproject(source=rasterio.band(src, 1), destination=destination_array,
                                      src_transform=src.transform, src_crs=wykryty_crs,
                                      dst_transform=dst_transform, dst_crs=dst_crs,
                                      resampling=Resampling.bilinear)
                            if src.nodata is not None and np.isnan(src.nodata):
                                destination_array = np.round(destination_array, 2)
                            else:
                                maska = destination_array != src.nodata
                                destination_array[maska] = np.round(destination_array[maska], 2)

                            dst.write(destination_array, 1)
                    
                    #---PLIK ZRODLOWY PL-2000 ZOSTAJE NA DYSKU---
                    #wczesniej byl usuwany zaraz po udanej reprojekcji, teraz
                    #zostaje w folderze tiff_pl2000 (dir_a) jako zapis danych
                    #w oryginalnym ukladzie, obok wersji przeliczonej w tiff_pl1992
                    print(f'[PROCES] Plik PL-2000 zachowany w: {tif_final_path}')

                    path_to_add = tif_reprojected_path
                    reproject_ok=True
                    print(f'[PROCES] Reprojekcja zakonczona: {os.path.basename(tif_reprojected_path)}')
                    
                except Exception as e:
                    reproject_ok=False
                    print(f"[PROCES] BLAD: Reprojekcja do PL-1992 nie powiodla sie: {e}")
                    continue

            #NOWY FRAGMENT - ODRZUCENIE PL2000 Z MOZAIKI
            if reproject_ok:
                #dodaje przetransformowany plik TIFF (na 100% w EPSG:2180) do listy mozaiki
                tiffs_to_mosaic.append(path_to_add)
            else:
                print(f'[PROCES] Plik {nazwa_pliku} nie zostal uwzgledniony w mozaice.')

    #FOLDER WYNIKOWY
    output_folder = os.path.join(os.path.dirname(final_output_path)) #, 'przekonwertowane')
    os.makedirs(output_folder, exist_ok=True)

    # ---5. PROSTOWANIE MACIERZY I REPROJEKCJI VRT/OPCJONALNE MOZAIKOWANIE---
    if not create_mosaic:
        #scenariusz bez mozaiki, tylko przetworzenie na tiff i pobranie
        '''
        output_folder=os.path.join(os.path.dirname(final_output_path), 'kafelki_1992')
        os.makedirs(output_folder, exist_ok=True)
        
        for f in tiffs_to_mosaic:
            #kopiuje pliki bazowe do folderu wynikowego
            base_name=os.path.basename(f).replace('_tmp.tif', '.tif')
            dest_path=os.path.join(output_folder, base_name)
            shutil.move(f, dest_path)
            '''
    
        print(f'[PROCES] {len(tiffs_to_mosaic)} plikow zapisano w: {output_folder}')

    else:
        #opcja TAK, tworzenie mozaiki (rozdzielczosc, mozaikowanie, przycinanie)
        print('\n[PROCES] Przetwarzanie rastrow do mozaiki.')

        #1. wyrownanie rozdzielczosci
        resolutions=[]
        for f in tiffs_to_mosaic:
            with rasterio.open(f) as src:
                resolutions.append(round(src.transform[0], 2))
        
        target_cellsize=max(resolutions)
        print(f'[PROCES] Najwiekszy wykryty piksel: {target_cellsize} m')
    
        cache_degraded_dir = os.path.join(dir_b, 'cache_degraded')
        os.makedirs(cache_degraded_dir, exist_ok=True)
        permanent_degraded_tiffs=[]

        #2. generowanie kafelkow o tej samej rozdzielczosci
        print(f'[PROCES] Zmiana rozdzielczosci kafelkow')
    
        for f in tiffs_to_mosaic:
            #BYLO: os.path.basename(f).replace('_tmp.tif', f'_res_{...}m.tif')
            #'_tmp.tif' nigdy nie wystepowalo w nazwie, wiec sufiks byl
            #doklejany na koniec ('plik.tif_res_0.5m.tif') - nieprawidlowa nazwa.
            #Dodatkowo usuwam sufiks '_2180' (dodany przy reprojekcji), zeby
            #get_date() nizej mogl prawidlowo odtworzyc oryginalna nazwe pliku .asc
            stem = Path(f).stem
            if stem.endswith('_2180'):
                stem = stem[:-len('_2180')]
            base_name = f'{stem}_res_{target_cellsize}m.tif'
            out_degraded_path = os.path.join(cache_degraded_dir, base_name)

            with rasterio.open(f) as src:
                #obl nowe wymiary na podstawie proporcji geograficznych
                #zapobiega splaszczeniu macierzy do 1x1 px
                src_width_m = src.bounds.right - src.bounds.left
                src_height_m = src.bounds.top - src.bounds.bottom
            
                new_width = int(round(src_width_m / target_cellsize))
                new_height = int(round(src_height_m / target_cellsize))
            
                new_width = max(1, new_width)
                new_height = max(1, new_height)

                new_transform = rasterio.transform.from_origin(
                    src.bounds.left, src.bounds.top, target_cellsize, target_cellsize)

                #konfiguruje raster VRT przekazujac parametry wyjsciowe (out_shape)
                with WarpedVRT(src,
                               crs=rasterio.crs.CRS.from_epsg(2180),
                               transform=new_transform,
                               width=new_width, height=new_height,
                               resampling=Resampling.bilinear) as vrt:
                
                    #!!!POPRAWKA CZYTANIA!!! 
                    #jawnie podaje ksztalt do odczytu, aby rasterio wiedziało, ze chce cala macierz pikseli!
                    degraded_data = vrt.read(1, out_shape=(new_height, new_width))

                    #---ZAOKRAGLENIE WYSOKOSCI DO 2 MSC PO PRZECINKU---
                    #interpolacja bilinear przy zmianie rozdzielczosci generuje wartosci z wieksza precyzja niz wejsciowe dane
                    if src.nodata is not None and np.isnan(src.nodata):
                        degraded_data = np.round(degraded_data, 2)
                    elif src.nodata is not None:
                        maska = degraded_data != src.nodata
                        degraded_data[maska] = np.round(degraded_data[maska], 2)
                    else:
                        degraded_data = np.round(degraded_data, 2)

                    vrt_meta = vrt.meta.copy()
                    vrt_meta.update({'driver': 'GTiff',
                                     'width': new_width, 'height': new_height,
                                     'transform': new_transform,
                                     'compress': 'lzw', 'nodata': src.nodata})

                    with rasterio.open(out_degraded_path, 'w', **vrt_meta) as dst:
                        dst.write(degraded_data, 1)
                
                permanent_degraded_tiffs.append(out_degraded_path)
        #print(f'[PROCES] Wygenerowano kafelki w cache')

        #3. tworzenie mozaiki
        def get_date(file_path):
            filename = os.path.basename(file_path).replace(f'_res_{target_cellsize}m.tif', '').replace('.tif', '.asc')
            #zwraca date ze slownika
            return mapa_daty.get(filename, pd.Timestamp.min)

        #---SORTOWANIE OD NAJNOWSZYCH---
        #mozaika ma sie wypelniac NAJNOWSZYMI danymi w pierwszej kolejnosci;
        #starsze dane dokladane sa tylko tam, gdzie nowsze nie pokrywaja JPT
        permanent_degraded_tiffs.sort(key=get_date, reverse=True)

        try:
            print(f'[PROCES] Laczenie rastrow')

            geom_shape = shape(geometry)
            jpt_bounds = geom_shape.bounds  #(minx, miny, maxx, maxy)
            temp_mosaic_path = os.path.join(dir_b, 'TEMP_big_mosaic.tif')

            #---MOZAIKOWANIE KAFELEK PO KAFELKU---
            #kafelki sa posortowane od najnowszych do najstarszych.
            #dokladam je w tej kolejnosci i po kazdym sprawdzam, JPT jest w 100% pokryte danymi
            #jak tak to przerywam, starsze kafelki nie sa potrzebne
            uzyte_tiffs = []
            mosaic = None
            out_trans = None
            out_meta = None
            out_image = None
            brakujace_px = 0
            pelne_pokrycie = False

            for idx, f in enumerate(permanent_degraded_tiffs):
                uzyte_tiffs.append(f)

                #odwracam kolejnosc dla merge(): najnowszy (pierwszy dolozony) ma byc na koncu
                kolejnosc_merge = list(reversed(uzyte_tiffs))
                src_files_to_mosaic = [rasterio.open(p) for p in kolejnosc_merge]

                mosaic, out_trans = merge(src_files_to_mosaic,
                                          bounds=jpt_bounds,
                                          res=target_cellsize)

                #---ZAOKRAGLENIE WYSOKOSCI DO 2 MSC PO PRZECINKU---
                mosaic = np.round(mosaic, 2)

                out_meta = src_files_to_mosaic[0].meta.copy()
                out_meta.update({'driver': 'GTiff',
                                 'height': mosaic.shape[1], 'width': mosaic.shape[2],
                                 'transform': out_trans,
                                 'crs': rasterio.crs.CRS.from_epsg(2180),
                                 'compress': 'lzw'})

                nodata_val = out_meta.get('nodata')

                #zamykam zrodla po kazdej iteracji
                for src in src_files_to_mosaic:
                    src.close()

                #---SPRAWDZAM POKRYCIE JPT---
                #zapisuje tymczasowo mozaike, przycinam do JPT i licze nodata
                with rasterio.open(temp_mosaic_path, 'w', **out_meta) as dest:
                    dest.write(mosaic)

                with rasterio.open(temp_mosaic_path) as src:
                    out_image, _ = mask(src, [geometry], crop=True)

                if nodata_val is not None:
                    if np.isnan(nodata_val):
                        brakujace_px = np.isnan(out_image).sum()
                    else:
                        brakujace_px = np.sum(out_image == nodata_val)
                else:
                    brakujace_px = 0

                print(f'[PROCES] Dolaczono kafelek {idx + 1}/{len(permanent_degraded_tiffs)} '
                      f'({os.path.basename(f)}) | brakujace px w JPT: {brakujace_px}')

                if brakujace_px == 0:
                    pelne_pokrycie = True
                    print(f'[PROCES] Obszar JPT w 100% pokryty danymi - przerywam mozaikowanie '
                          f'({len(permanent_degraded_tiffs) - (idx + 1)} starszych kafelkow pominieto).')
                    break

            if pelne_pokrycie:
                print('[PROCES] Mozaika zapelniona przed wykorzystaniem wszystkich kafelkow.')
            else:
                #---BRAK PELNEGO POKRYCIA NAWET PO WYKORZYSTANIU WSZYSTKICH KAFELKOW---
                if out_image is not None and out_image.size > 0:
                    procent_braku = round(100 * brakujace_px / out_image.size, 2)
                else:
                    procent_braku = None

                if procent_braku is not None:
                    print(f'[PROCES] UWAGA: wykorzystano wszystkie {len(permanent_degraded_tiffs)} kafelkow.'
                          f'Obszar JPT nie jest w 100% pokryty danymi ')
                else:
                    print('[PROCES] Wykorzystano wszystkie kafelki, obszar JPT pokryty w 100%.')

            #4. przycinanie do JPt (finalny zapis)
            print('[PROCES] Przycinanie do ksztaltu JPT')
            with rasterio.open(temp_mosaic_path) as src:
                out_image, out_transform = mask(src, [geometry], crop=True)

                #---ZAOKRAGLENIE WARTOSCI PIKSELI (WYSOKOSCI) DO 2 MSC PO PRZECINKU---
                #np.round() bezpiecznie zwraca NaN ani nie psuje -9999
                out_image = np.round(out_image, 2)

                out_meta.update({'height': out_image.shape[1], 'width': out_image.shape[2], 'transform': out_transform})
                with rasterio.open(final_output_path, 'w', **out_meta) as dest:
                    dest.write(out_image)

            #sprzatanie
            if os.path.exists(temp_mosaic_path):
                os.remove(temp_mosaic_path)
            for f in permanent_degraded_tiffs:
                if os.path.exists(f): os.remove(f)

            #usuwam folder cache_degraded jesli jest juz pusty
            try:
                if os.path.isdir(cache_degraded_dir) and not os.listdir(cache_degraded_dir):
                    os.rmdir(cache_degraded_dir)
                    print(f'[PROCES] Usunieto pusty folder: {cache_degraded_dir}')
            except Exception as e:
                print(f'[PROCES] Nie udalo sie usunac folderu {cache_degraded_dir}: {e}')

            print(f'[PROCES] Wynik (mozaika) zapisany w: {final_output_path}')

        except Exception as e:
            print(f'[PROCES] Blad podczas mozaikowania: {e}')

    #---SPRZATANIE PUSTEGO FOLDERU tiff_pl2000 (dir_a)---
    #jesli jest pusty kiedy nie pobrano zadnych plikow PL-2000
    try:
        if dir_a != zip_dir and os.path.isdir(dir_a) and not os.listdir(dir_a):
            os.rmdir(dir_a)
            print(f'[PROCES] Usunieto pusty folder: {dir_a}')
    except Exception as e:
        print(f'[PROCES] Nie udalo sie usunac folderu {dir_a}: {e}')