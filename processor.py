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
        print(f"[TESTOWE] Blad podczas analizy pliku {os.path.basename(file_path)}: {e}")
        
    # Bezpiecznik: w razie nietypowej struktury zwracamy domyślny układ krajowy PL-1992
    print(f"[TESTOWE] Nie mozna zidentyfikowac EPSG pliku {os.path.basename(file_path)}, przyjeto domyslnie 2180")
    return CRS.from_epsg(2180)


try:
    from unzip import unzip
except ImportError:
    def unzip(zip_p, extract_p):
        with zipfile.ZipFile(zip_p, 'r') as zip_ref:
            zip_ref.extractall(extract_p)

from AIAG2GT import AIAG2GTIFF
from ASCII2GT import ASCII2GT

def process_data(zip_dir, final_output_path, geometry, mapa_ukladow=None):
    if mapa_ukladow is None:
        mapa_ukladow = {}

    # ---1. ROZPAKOWANIE WSZYSTKICH ZIP ---
    zip_files = glob.glob(os.path.join(zip_dir, '*.zip'))
    for z_path in zip_files:
        print (f'Rozpakowywanie pliku {os.path.basename(z_path)}')
        with zipfile.ZipFile(z_path, 'r') as zip_ref:
            zip_ref.extractall(zip_dir)
    
    #!!!WAZNE!!! daje czas na pelne wczyatnie danych na dysk
    time.sleep(2.0)
        
    print('Rozpakowano pliki zrodlowe')

    #--- 2. SZUKANIE DOSTEPNYCH FORMATOW NMT ---
    tiffs_to_mosaic = []
    
    #szukam mozliwych rozszerzen
    wyszukane_pliki = []
    for ext in ['*.xyz', '*.asc', '*.txt']:
        wyszukane_pliki.extend(glob.glob(os.path.join(zip_dir, ext)))
        wyszukane_pliki.extend(glob.glob(os.path.join(zip_dir, ext.upper())))

    if not wyszukane_pliki:
        print("[PROCES] Nie znaleziono NMT (.xyz, .asc, .txt) po rozpakowaniu")
        return

    print(f"\n[PROCES] Wykryto {len(wyszukane_pliki)} plikow")
    
    for rfile in wyszukane_pliki:
        nazwa_pliku = os.path.basename(rfile)
        tif_tmp_path = os.path.join(zip_dir, nazwa_pliku.rsplit('.', 1)[0] + '_tmp.tif')

        #tutaj jeszcze praca nad epsg
        kod_epsg=mapa_ukladow.get(nazwa_pliku, 2180)
        wykryty_crs = CRS.from_epsg(kod_epsg)

        print(f"[PROCES] -> Plik: {nazwa_pliku} | EPSG:{kod_epsg}")

        #---URUCHAMIANIE KONWERTEROW Z PRZEKAZANIEM CRS---
        try:
            with open(rfile, 'r', encoding='utf-8') as f:
                #czytam pierwsza linie dla spr pliku (grid czy inny) lub format
                first_line = f.readline().lower()
            
            if 'ncols' in first_line:
                AIAG2GTIFF(rfile, tif_tmp_path, epsg_code=kod_epsg)
            else:
                ASCII2GT(rfile, tif_tmp_path, epsg_code=kod_epsg)
        
        except Exception as e:
            print(f"[PROCES] BLAD! Konwersja pliku {nazwa_pliku} sie nie powiodal: {e}")
            continue

        #---4. REPROJEKCJA DO 1992 W PRZYPADKU PL-2000---
        if os.path.exists(tif_tmp_path):
            if kod_epsg != 2180:
                tif_reprojected_path = tif_tmp_path.replace('_tmp.tif', '_reprojected_tmp.tif')
                dst_crs = CRS.from_epsg(2180)
                
                try:
                    with rasterio.open(tif_tmp_path) as src:
                        #przeliczenie transformacji i wymiarow siatki
                        dst_transform, dst_width, dst_height = calculate_default_transform(wykryty_crs,
                                                                                           dst_crs,
                                                                                           src.width,
                                                                                           src.height,
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
                            dst.write(destination_array, 1)
                    
                    #usuwam plik zrodlowy 2000 i zastepuje 1992
                    os.remove(tif_tmp_path)
                    os.rename(tif_reprojected_path, tif_tmp_path)
                    
                except Exception as e:
                    print(f"[PROCES] BLAD: Reprojekcja do PL-1992 nie powiodla sie: {e}")
                    continue

            #dodaje przetransformowany plik TIFF (na 100% w EPSG:2180) do listy mozaiki
            tiffs_to_mosaic.append(tif_tmp_path)

    # --- 5. POPRAWIONE MOZAIKOWANIE (PROSTOWANIE MACIERZY I REPROJEKCJI VRT) ---
    if not tiffs_to_mosaic:
        print('[PROCES] Nie utworzono plikow TIFF do polaczenia')
        return

    resolutions = []
    for f in tiffs_to_mosaic:
        with rasterio.open(f) as src:
            resolutions.append(round(src.transform[0], 2))
    target_cellsize = max(resolutions)
    
    print(f"\n[PROCES] Najwiekszy wykryty piksel: {target_cellsize} m")

    geom_shape = shape(geometry)
    jpt_bounds = geom_shape.bounds  # (minx, miny, maxx, maxy)
    
    cache_degraded_dir = os.path.join(zip_dir, 'cache_degraded')
    os.makedirs(cache_degraded_dir, exist_ok=True)

    permanent_degraded_tiffs = []

    print("\n[PROCES] Zmiana rozdzielczosci kafelkow")
    for f in tiffs_to_mosaic:
        base_name = os.path.basename(f).replace('_tmp.tif', f'_res_{target_cellsize}m.tif')
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
                #Musimy jawnie podać kształt do odczytu, aby rasterio wiedziało, że chcemy całą macierz pikseli!
                degraded_data = vrt.read(1, out_shape=(new_height, new_width))
                
                vrt_meta = vrt.meta.copy()
                vrt_meta.update({'driver': 'GTiff',
                                 'width': new_width, 'height': new_height,
                                 'transform': new_transform,
                                 'compress': 'lzw', 'nodata': src.nodata})

                with rasterio.open(out_degraded_path, 'w', **vrt_meta) as dst:
                    dst.write(degraded_data, 1)
                
        permanent_degraded_tiffs.append(out_degraded_path)

    print(f"[PROCES] Wygenerowano kafelki w cache")

    src_files_to_mosaic = []
    temp_mosaic_path = os.path.join(zip_dir, 'TEMP_big_mosaic.tif')

    try:
        for f in permanent_degraded_tiffs:
            src = rasterio.open(f)
            src_files_to_mosaic.append(src)

        print(f"\n[PROCES] Laczenie {len(src_files_to_mosaic)} arkuszy")
        
        #alcze w sztywnych granicach JPT
        mosaic, out_trans = merge(src_files_to_mosaic,
                                  bounds=jpt_bounds,
                                  res=target_cellsize)
        
        out_meta = src_files_to_mosaic[0].meta.copy()
        out_meta.update({'driver': 'GTiff',
                         'height': mosaic.shape[1], 'width': mosaic.shape[2],
                         'transform': out_trans, 
                         'crs': rasterio.crs.CRS.from_epsg(2180),
                         'compress': 'lzw'})

        with rasterio.open(temp_mosaic_path, 'w', **out_meta) as dest:
            dest.write(mosaic)

        for src in src_files_to_mosaic:
            src.close()
        src_files_to_mosaic = []

        # ---5. PRZYCINANIE DO GRANIC JPT ---
        print('[PROCES] Przycinanie do ksztaltu JPT')
        with rasterio.open(temp_mosaic_path) as src:
            out_image, out_transform = mask(src, [geometry], crop=True)
            out_meta = src.meta.copy()
            out_meta.update({'driver': 'GTiff',
                             'height': out_image.shape[1], 'width': out_image.shape[2],
                             'transform': out_transform, 'nodata': src.nodata})

        with rasterio.open(final_output_path, 'w', **out_meta) as dest:
            dest.write(out_image)

        print(f'[PROCES] Wynik zapisany w: {final_output_path}')

    except Exception as e:
        print(f'[PROCES] Blad podczas mozaikowania/maskowania: {e}')

    # ---6. SPRZATANIE PLIKOW TYMCZASOWYCH---
    #ta czesc czasem nie dziala ale nie przeszkadza w glownym procesie
    finally:
        #naa wszelki wypadek zamykam otwarte strumienie
        for src in src_files_to_mosaic:
            try: src.close()
            except: pass
            
        print('[PROCES] Czyszczenie plikow tymczasowych')
        if os.path.exists(temp_mosaic_path):
            os.remove(temp_mosaic_path)
        for f in tiffs_to_mosaic:
            if os.path.exists(f):
                os.remove(f)