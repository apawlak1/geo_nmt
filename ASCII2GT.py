# -*- coding: utf-8 -*-
'''
Created on Mon Apr  6 17:52:41 2026

@author: olaa3
'''

import numpy as np
import rasterio
from rasterio.transform import from_origin
from rasterio.crs import CRS
from pathlib import Path
import zipfile

def ASCII2GT(input_path, output_path, epsg_code=2180):
    input_p = Path(input_path)
    
    if input_p.suffix.lower()=='.zip':
        print('[KONWERTER ASCII] Wypakowywanie ZIP')
    
        #tworzenie folderu jak nie istnieje
        extraction_folder = input_p.with_suffix('')
        extraction_folder.mkdir(parents=True, exist_ok=True)
    
        with zipfile.ZipFile(input_path, 'r') as zip_ref:
            zip_ref.extractall(extraction_folder)
        
        print(f"[KONWERTER ASCII] Wypakowano do: {extraction_folder}")

        found_files = [f for f in extraction_folder.iterdir() if f.suffix.lower() in ['.asc', '.txt', '.xyz']]
        if not found_files:
            print(f'[KONWERTER ASCII] Brak plikow ASCII w folderze {extraction_folder}')
            return None
        
        #podmieniam sciezke wejsciowa na rozpakowany plik
        new_input=found_files[0]
    else:
        new_input=input_p
    

    #---KONWERTUJE FORMATY ASCII (NMT, TBD, XYZ GRID) DO FORMATU GEOTIFF---
    with open(new_input, 'r') as file:
        txtfile=file.readlines()

    body=[]
    for line in txtfile:
        #czyszcze linie z bialych znakow i dziele
        line_parts = line.strip().split()
        
        #ignoruje naglowki i puste linie, tylko wiersze z 3 kolumnami
        if len(line_parts) == 3:
            try:
                #zeby odsiac tekst
                float_parts = [float(x) for x in line_parts]
                body.append(float_parts)
            except ValueError:
                continue

    #spr czy cokolwiek sie wczytalo
    if len(body) == 0:
        print(f"[KONWERTER ASCII] BLAD: Plik {new_input} nie zawiera poprawnych linii X Y Z.")
        return None

    # Tworzymy array i wymuszamy, żeby był 2D
    body = np.array(body, dtype=np.float64)
    
    # DODATKOWE SPRAWDZENIE STRUKTURY
    if body.ndim != 2 or body.shape[1] != 3:
        print(f"[KONWERTER ASCII] BLAD: Nieprawidlowa struktura danych w {new_input}, shape: {body.shape}")
        return None

    x_coords=body[:,0]
    y_coords=body[:,1]
    z_coords=body[:,2]
    
    #--- 1. CELLSIZE---
    #zaokraglam wsp wdo 3 miejsc po przecinku zeby uniknac bledu precyzji Pythona
    unique_x = np.sort(np.unique(np.round(x_coords, 3)))
    unique_y = np.sort(np.unique(np.round(y_coords, 3)))
    cellsize = None
    
    #spr roznice miedzy unikalnymi kolumnami (os X)
    if len(unique_x) > 1:
        diffs_x = np.diff(unique_x)
        #biore najmniejsza rzeczywista roznice wieksza niz 5 cm (najmniejszy mozliwy px w PZGiK)
        valid_diffs_x = diffs_x[diffs_x > 0.05]
        if len(valid_diffs_x) > 0:
            cellsize = round(np.min(valid_diffs_x), 2) #zaokr do cm

    #spr os Y jesli pkt w pliku to pionowa linia (mozliwe w ASCII TBD)
    if cellsize is None and len(unique_y) > 1:
        diffs_y = np.diff(unique_y)
        valid_diffs_y = diffs_y[diffs_y > 0.05]
        if len(valid_diffs_y) > 0:
            cellsize = round(np.min(valid_diffs_y), 2)

    # Ostateczne zabezpieczenie przed zerem lub brakiem wykrycia kroków siatki
    if cellsize is None or cellsize <= 0:
        print(f"[KONWERTER ASCII] Pominieto (nieprawidłowa struktura siatki punktów): {new_input.name}")
        return None

    x_min, x_max = x_coords.min(), x_coords.max()
    y_min, y_max = y_coords.min(), y_coords.max()    

    #--- 2. INDEKSOWANIE (ALOKACJA RAM) ---
    #obl liczbe kolumn i wierszy
    ncols = int(round((x_max - x_min) / cellsize)) + 1
    nrows = int(round((y_max - y_min) / cellsize)) + 1
    
    #przygotowanie matrycy rastra
    raster = np.full((nrows, ncols), np.nan, dtype=np.float64)
    
    #WAZNE!!! najpierw zaokraglam do najnizszej liczby calkowitej, dopiero pozniej wrzucam do int
    #chodzi o precyzje Pythona
    cols = np.round((x_coords - x_min) / cellsize).astype(int)
    rows = np.round((y_max - y_coords) / cellsize).astype(int)

    cols = np.clip(cols, 0, ncols - 1)
    rows = np.clip(rows, 0, nrows - 1)

    #wypelnienie rastra wartosciami Z
    raster[rows, cols] = z_coords

    #--- 3. BOUNDING BOX DLA RASTERIO---
    x_corner = x_min - (cellsize / 2)  
    y_corner = y_max + (cellsize / 2)

    transform=from_origin(x_min, y_max, cellsize, cellsize)
    wykryty_crs = CRS.from_epsg(epsg_code)

    #---!!!ZMIANA TIFFFILE NA RASTERIO!!!---
    with rasterio.open(output_path, 'w', driver='GTiff',
                       height=raster.shape[0], width=raster.shape[1], count=1,
                       dtype='float32',
                       crs=wykryty_crs, transform=transform,
                       nodata=np.nan, compress='lzw') as dst:
        dst.write(raster.astype('float32'), 1)

    print(f'[KONWERTER ASCII] Zapisano w: {output_path}')

    print(f"[KONWERTER ASCII] Oryginalne Z min/max: {z_coords.min()} / {z_coords.max()}")
    print(f"[KONWERTER ASCII] Raster Z min/max: {np.nanmin(raster)} / {np.nanmax(raster)}")
    
    return cellsize, raster


#---MOJA CZESC TESTOWA---

#ASCII NMT
#testfileN=r'C:\Users\olaa3\Desktop\SKOROWIDZE\ASCII\ASCII_NMT\72973_890100_NMT-M-34-3-B-b-2-4.zip'
#outputN=r'C:\Users\olaa3\Desktop\SKOROWIDZE\cache\eksperyment_NMT.tif'
#test=ASCII2GT(testfileN, outputN)

#ASCII TBD
#testfileT=r'C:\Users\olaa3\Desktop\SKOROWIDZE\ASCII\ASCII_TBD\73727_1018381_6.161.33.03.1.zip'
#outputT=r'C:\Users\olaa3\Desktop\SKOROWIDZE\cache\eksperyment_TBD.tif'
#test=ASCII2GT(testfileT, outputT)

#ASCII XYZ GRID
#testfileX=r'C:\Users\olaa3\Desktop\SKOROWIDZE\ASCII\ASCII_XYZ_GRID\73853_1042043_M-34-7-B-b-2-2.xyz'
#outputX=r'C:\Users\olaa3\Desktop\SKOROWIDZE\cache\eksperyment_XYZ.tif'
#test=ASCII2GT(testfileX, outputX)

