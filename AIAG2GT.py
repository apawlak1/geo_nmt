# -*- coding: utf-8 -*-
'''
Created on Mon Apr  6 16:46:35 2026

@author: olaa3
'''

import numpy as np
import rasterio
from rasterio.transform import from_origin
from rasterio.crs import CRS

testfile=r'C:\Users\olaa3\Desktop\SKOROWIDZE\ARC_INFO_ASCII_GRID\81711_1672275_6.163.32.23.3.asc'
output_path=r'C:\Users\olaa3\Desktop\SKOROWIDZE\cache\81711_1672275_6.163.32.23.3.tif'

def AIAG2GTIFF(input_path, output_path, epsg_code=2180):
    #---KONWERTUJE FORMAT ARC INFO ASCII GRID DO FORMATU GEOTIFF---
    with open(input_path, 'r') as file:
        lines=file.readlines()

    #wychodze z pliku, pracuje na tym co jest w pamieci    
    header=[line.split() for line in lines[:6]]
    body=lines[6:]

    #metadata: przerabiane naglowka na zmienne
    #[0] na koncu wyciaga wszystko do zwyklej zmiennej (bez listy)
    md={row[0].lower(): float(row[1]) for row in header}

    ncols = int(md['ncols'])
    nrows = int(md['nrows'])
    xllcenter = md['xllcenter']
    yllcenter = md['yllcenter']
    cellsize = round(md['cellsize'], 2)
    nodata_value = md.get('nodata_value', -9999) #.get na wypadek braku klucza

    #--- PRZELICZENIE DLA RASTERIO ---
    #xllcenter i yllcenter to SRODEK lewego dolnego px
    #a potrzebuje wsp ZEWNETRZNEGO LEWEGO GORNEGO ROGU (x_left, y_top).
    x_left = xllcenter - (cellsize / 2)
    
    #przejscie od dolnego srodka (yllcenter) do gornej krawędzi (y_top)
    y_top = yllcenter + (nrows - 1) * cellsize + (cellsize / 2)

    #zmiana tresci (body) na macierz numpy (2D)
    data = np.fromstring(' '.join(body), sep=' ').reshape((nrows, ncols)).astype('float32')

    #definicja transformacji przestrzennej na podstawie lewego gornego rogu
    transform = from_origin(x_left, y_top, cellsize, cellsize)
    wykryty_crs = CRS.from_epsg(epsg_code)

    #---ZAPIS ZA POMOCĄ RASTERIO (ZAMIAST TIFFFILE)---
    with rasterio.open(output_path, 'w', driver='GTiff',
                       height=nrows, width=ncols, count=1, dtype='float32',
                       crs=wykryty_crs, 
                       transform=transform, nodata=nodata_value, compress='lzw' #lzw jest bezstratna
                       ) as dst:
        dst.write(data, 1)

    print(f'[KONWERTER AIAG] Zapisano w: {output_path}')
    
#test=AIAG2GTIFF(testfile, output_path)