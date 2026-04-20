import numpy as np
import tifffile
import os
from pathlib import Path
import zipfile

def ASCII2GT(input_path, output_path):
    inp=Path(input_path)

    #'folder' do zapisywania danych
    all_data=[]
    input_files=[]

    #---CZESC ZIPOWA---
    if inp.suffix.lower()=='.zip':
        print(f'Wypakowywanie ZIP')
        ex_folder=inp.with_suffix('')
        ex_folder.mkdir(parents=True, exist_ok=True)

        with zipfile.ZipFile(input_path, 'r') as zip_ref:
            zip_ref.extractall(ex_folder)

            #za pomoca rglob szukam wszedzie, nawet w podfolderach
        input_files = [f for f in ex_folder.rglob('*') if f.suffix.lower() in ['.asc', '.txt', '.xyz']]
        print(f"Znaleziono plików w ZIP: {len(input_files)}")
    else:
        input_files=[inp]

    if not input_files:
        print('Brak jakichkolwiek danych')
        return None
    
    #---POBRANIE DANYCH Z WSZYSTKICH PLIKOW---
    for fpath in input_files:
        print(f'Wczytywanie plikow')
        with open(fpath, 'r') as f:
            for line in f:
                # strip() usuwa entery i spacje z końców, split() dzieli po białych znakach
                parts = line.strip().split()
                
                # KLUCZOWE: Bierzemy tylko linie, które mają dokładnie 3 kolumny liczbowe
                if len(parts) == 3:
                    try:
                        if fpath.suffix.lower()=='.txt':
                            parts[0], parts[1] = parts[1], parts[0]
                            row = [float(x) for x in parts]
                            all_data.append(row)
                        else:
                        # Próbujemy zamienić na floaty. Jeśli to tekst (np. "Start"), rzuci błąd i przejdzie dalej
                            row = [float(x) for x in parts]
                            all_data.append(row)
                    except ValueError:
                        continue
    
    #---ROZPOZNANIE UKLADU WSP PLASKICH
    #ustawiam domyslnie PL-1992, pozniej najwyzej bedzie zmiana
    epsg_code=2180

    fname=str(fpath.name).upper()
    if 'M-33' in fname or 'M-34' in fname or 'N-33' in fname or 'N-34' in fname:
        epsg_code=2180
    elif '_5.' in fname:
        epsg_code=2176
    elif '_6.' in fname:
        epsg_code=2177
    elif '_7.' in fname:
        epsg_code=2178
    elif '_8.' in fname:
        epsg_code=2179

    #wszystko do jednej tablicy np
    body = np.array(all_data, dtype=np.float64)
 
    x_coords=body[:,0]
    y_coords=body[:,1]
    z_coords=body[:,2]

    #granice i rozmiar macierzy
    x_min, x_max=x_coords.min(), x_coords.max()
    y_min, y_max=y_coords.min(), y_coords.max()
    
    #obliczanie rozdzielczosci
    if x_coords[1]-x_coords[0] != 0:
        cellsize=x_coords[1]-x_coords[0]
    else:
        cellsize=y_coords[1]-y_coords[0]

    #obliczenie liczby kolumn i wierszy
    ncols=int((x_max-x_min)/cellsize)+1
    nrows=int((y_max-y_min)/cellsize)+1
    
    #pusta macierz do wypelnienia
    raster=np.full((nrows, ncols), np.nan, dtype=np.float64)
    
    # 4. Mapowanie punktów na indeksy macierzy
    # Obliczamy, w którym wierszu i kolumnie ma znaleźć się dany punkt Z
    cols = ((x_coords - x_min) / cellsize).astype(int)
    rows = ((y_max - y_coords) / cellsize).astype(int) # Odwracamy Y, bo TIFF rośnie w dół

    # Wypełnienie rastra wartościami Z
    raster[rows, cols]=z_coords

    # 5. Georeferencja (Tiepoint to lewy górny róg)
    # Ponieważ X i Y w pliku to środki pikseli, przesuwamy o pół komórki do rogu
    x_corner = x_min - (cellsize / 2)
    y_corner = y_max + (cellsize / 2)

    # Tagi dla układu współrzędnych (GeoKeyDirectoryTag)
    # 1, 1, 0, 7 -> Nagłówek
    # 1024, 0, 1, 1 -> Model Type (1 = Projected)
    # 1025, 0, 1, 1 -> Raster Type (1 = Area / PixelIsArea)
    # 2048, 0, 1, epsg_code -> Tu wstawiamy nasz kod EPSG (np. 2180)
    
    geokeys = [1, 1, 0, 4, 
               1024, 0, 1, 1, 
               1025, 0, 1, 1, 
               2048, 0, 1, epsg_code,
               3072, 0, 1, epsg_code]
    
    #definicja tagow geotiff
    # 33550: ModelPixelScaleTag [x, y, z]
    # 33922: ModelTiepointTag [i, j, k, x, y, z]
    pixel_scale = [cellsize, cellsize, 0]
    tiepoint = [0, 0, 0, x_corner, y_corner, 0]

    # Rejestrujemy tagi w formacie akceptowanym przez nowsze tifffile
    # (Kod_Taga, Typ, Liczba_elementów, Wartości, Czy_pisać_bezpośrednio)
    extra_tags = [(33550, 'd', 3, pixel_scale, True),
                  (33922, 'd', 6, tiepoint, True),
                  (34735, 'H', len(geokeys), geokeys, True)]

    #zapis za pomoca tifffile
    #minisblack (minimum is black) daje programowi informacje jak ma interpretowac go fotometrycznie
    tifffile.imwrite(output_path, raster, photometric='minisblack', extratags=extra_tags)

    print(f'Zapisano w: {output_path}')

    print(f"Oryginalne Z min/max: {z_coords.min()} / {z_coords.max()}")
    print(f"Raster Z min/max: {np.nanmin(raster)} / {np.nanmax(raster)}")
    
    return cellsize, raster


#---MOJA CZESC TESTOWA---

#ASCII NMT
#testfileN=r'C:\Users\olaa3\Desktop\SKOROWIDZE\ASCII\ASCII_NMT\72973_890100_NMT-M-34-3-B-b-2-4.zip'
#outputN=r'C:\Users\olaa3\Desktop\SKOROWIDZE\cache\eksperyment_NMT.tif'
#test=ASCII2GT(testfileN, outputN)

#ASCII TBD
testfileT=r'C:\Users\olaa3\Desktop\SKOROWIDZE\ASCII\ASCII_TBD\73727_1018381_6.161.33.03.1.zip'
outputT=r'C:\Users\olaa3\Desktop\SKOROWIDZE\cache\eksperyment_TBD.tif'
test=ASCII2GT(testfileT, outputT)

#ASCII XYZ GRID
#testfileX=r'C:\Users\olaa3\Desktop\SKOROWIDZE\ASCII\ASCII_XYZ_GRID\73853_1042043_M-34-7-B-b-2-2.xyz'
#outputX=r'C:\Users\olaa3\Desktop\SKOROWIDZE\cache\eksperyment_XYZ.tif'
#test=ASCII2GT(testfileX, outputX)