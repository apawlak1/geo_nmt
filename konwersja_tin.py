# -*- coding: utf-8 -*-

'''
Created on Mon Apr 20 18:00:00 2026
@author: olaa3
'''

import arcpy
import json
import os
from pathlib import Path

config_file = Path(__file__).with_name('config.json')
config = {}
if config_file.exists():
    with open(config_file, 'r', encoding='utf-8') as f:
        config = json.load(f)

tin_config = config.get('konwersja_tin', {})

# scieżka do folderu z TIN (ESRI TIN to folder, nie pojedynczy plik)
testfile = tin_config.get('input_tin_folder', r'C:\Users\olaa3\Desktop\SKOROWIDZE\ESRI_TIN\4498_378005_N-34-118-B-a-4_tin')
output_path = tin_config.get('output_tif', r'C:\Users\olaa3\Desktop\SKOROWIDZE\cache\4498_378005_N-34-118-B-a-4.tif')
cellsize = float(tin_config.get('cellsize', 0.5))

def TIN2GT(input_path, output_path, cellsize=1.0):
    #---KONWERTUJE FORMAT ASCII NMT DO FORMATU GEOTIFF---
    #WAZNE, MUSI BYC MOZLIWOSC NADPISYWANIA
    arcpy.env.overwriteOutput = True
    
    #Metoda interpolacji LINEAR (liniowa) lub NATURAL_NEIGHBORS
    #Z_FACTOR: 1 (brak przeskalowania w pionie)
    arcpy.ddd.TinRaster(in_tin=input_path,
        out_raster=output_path,
        data_type="FLOAT",
        method="LINEAR",
        sample_distance="CELLSIZE " + str(cellsize),
        z_factor=1)

    print(f'Zapisano w: {output_path}')

# Wywołanie funkcji
tin_cellsize = float(tin_config.get('cellsize', 0.5))
TIN2GT(testfile, output_path, cellsize=tin_cellsize)