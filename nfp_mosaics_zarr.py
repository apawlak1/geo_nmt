# -*- coding: utf-8 -*-
'''
BUDOWA WIELOCZASOWEJ BAZY NMT W ZARR, BLOKAMI, W KONWENCJI CZYTELNEJ DLA GIS

Rozwiniecie dotychczasowego nfp_mosaics_zarr.py:
  - _siatka i _zbuduj_blok zostaly (kod Twoj; male zmiany opisane przy funkcjach),
  - zbuduj_baze_zarr / zbuduj_baze_wieloczasowa_zarr (budowa CALEGO store'u naraz) zastapione przez
    zbuduj_warstwe_bazowa: buduje JEDNA warstwe (rok) w istniejacym store'ze i zapisuje OBIE zmienne:
    nmt (wysokosc) oraz rozdzielczosc_zrodla (rozdzielczosc arkusza, z ktorego pochodzi piksel);
    _zbuduj_blok (Twoj) zostal jako punkt odniesienia w testach, a produkcyjnie blok buduje
    _zbuduj_blok_ze_zrodlem (ten sam wynik dla wysokosci + rozdzielczosc zrodla); store tworzenie,
    dopisywanie lat, poziomy i aktualizacja przyrostowa sa w zarr_magazyn.py / zarr_poziomy.py /
    zarr_aktualizacja.py

Arkusze sa sortowane od NAJNOWSZYCH; piksel bierze wartosc z pierwszego arkusza, ktory go
pokrywa (rasterio.merge, method='first', nearest) - to zastepuje test pokrycia JPT.
Piksele poza granica powiatu = NaN.

WYMAGA: xarray, rioxarray, dask (oprocz juz uzywanych rasterio/numpy/shapely)
'''

import math

import numpy as np
import rasterio
import rasterio.windows
from rasterio.merge import merge
from rasterio.enums import Resampling
from rasterio.transform import from_origin
from rasterio.features import geometry_mask
from shapely.geometry import shape, box

from nfp_mosaics import wyrownanie_do_siatki
from zarr_magazyn import Siatka, przecina


def _siatka(geometry, native_cellsize, faktory=()):
    #---GEOMETRIA + SIATKA + WSPOLRZEDNE SRODKOW PIKSELI (standard GIS/xarray,
    #NIE naroznikow) - to wlasnie te tablice x/y GDAL/QGIS/ArcGIS odczytuja
    #jako georeferencje.---
    #ZMIANA: opcjonalny parametr 'faktory' (faktory poziomow rozdzielczosci, np. [10, 20, 40]) -
    #granice sa wyrownane do NWW faktorow * piksel, dzieki czemu wymiary bazy dziela sie przez
    #kazdy faktor, a piksel poziomu = dokladnie f x f pikseli bazy
    nww=1
    for f in faktory:
        nww=nww * f // math.gcd(nww, f)
    krok=round(native_cellsize * nww, 6)

    geom_shape=geometry if hasattr(geometry, 'bounds') else shape(geometry)
    jpt_bounds=geom_shape.bounds
    left, bottom, right, top=wyrownanie_do_siatki(jpt_bounds, krok)
    width=max(nww, int(round((right - left) / native_cellsize)))
    height=max(nww, int(round((top - bottom) / native_cellsize)))
    dst_transform=from_origin(left, top, native_cellsize, native_cellsize)

    xs=left + (np.arange(width) + 0.5) * native_cellsize
    ys=top - (np.arange(height) + 0.5) * native_cellsize
    return geom_shape, dst_transform, width, height, xs, ys


def siatka_z_geometrii(geometry, native_cellsize, faktory=(), epsg=2180):
    #---TO SAMO CO _siatka, ALE ZWRACA OBIEKT Siatka (do zapisu w store'ze)---
    _, tr, width, height, _, _=_siatka(geometry, native_cellsize, faktory)
    return Siatka(round(tr.c, 6), round(tr.f, 6), float(native_cellsize), width, height, epsg)


def _zbuduj_blok(zrodla_bounds, win_bounds, win, native_cellsize, geom_shape, wypelnienie):
    zrodla_w_bloku=[p for p, b in zrodla_bounds
                      if not (b.right < win_bounds[0] or b.left > win_bounds[2]
                              or b.top < win_bounds[1] or b.bottom > win_bounds[3])]
    if not zrodla_w_bloku:
        return None  #---blok pusty - zostaje z fill_value, nic nie zapisujemy---

    srcs=[rasterio.open(p) for p in zrodla_w_bloku]
    try:
        #---method='first' (jawnie): piksel bierze wartosc z PIERWSZEGO zrodla
        #na liscie ktore go pokrywa i NIE jest nadpisywany przez kolejne -
        #stad lista zrodla_bounds MUSI byc posortowana najnowsze->najstarsze
        #(patrz sortowanie w wersja_dev.py). To zastepuje test pokrycia JPT.---
        mos_blok, blok_transform=merge(srcs, bounds=win_bounds, res=native_cellsize,
                            resampling=Resampling.nearest, method='first',
                            target_aligned_pixels=True)
    finally:
        for s in srcs:
            s.close()

    #ZMIANA: merge moze zwrocic minimalnie inny rozmiar niz okno - wpisuje wynik w okno o
    #docelowym rozmiarze (reszta = brak danych), zeby zapis do tablicy zawsze mial dobry ksztalt
    blok=np.full((win.height, win.width), np.nan, dtype='float32')
    h, w=min(win.height, mos_blok.shape[1]), min(win.width, mos_blok.shape[2])
    blok[:h, :w]=mos_blok[0, :h, :w]
    mos_blok=blok
    blok_transform=from_origin(win_bounds[0], win_bounds[3], native_cellsize, native_cellsize)

    gmask=geometry_mask([geom_shape], out_shape=mos_blok.shape,
                          transform=blok_transform, invert=True)
    return np.where(gmask, mos_blok, wypelnienie)

def _zbuduj_blok_ze_zrodlem(zrodla, win_bounds, win, native_cellsize, geom_shape):
    '''
    To samo co _zbuduj_blok (merge 'first', nearest, najnowsze arkusze pierwsze), ale kazdy arkusz jest scalany osobno,
    dzieki czemu wiadomo, ktory dal dany piksel. Piksel bierze wartosc z PIERWSZEGO arkusza (od najnowszego),
    ktory ma w nim dane - dokladnie jak w merge(method='first').

    zrodla : lista (sciezka, bounds, rozdzielczosc) POSORTOWANA najnowsze->najstarsze
    zwraca (wysokosc, rozdzielczosc_zrodla) - dwie tablice float32 rozmiaru okna, poza granica NaN;
           (None, None), gdy zaden arkusz nie przecina okna
    '''
    w_bloku=[(p, b, r) for p, b, r in zrodla
             if not (b.right < win_bounds[0] or b.left > win_bounds[2]
                     or b.top < win_bounds[1] or b.bottom > win_bounds[3])]
    if not w_bloku:
        return None, None

    blok=np.full((win.height, win.width), np.nan, dtype='float32')
    res=np.full((win.height, win.width), np.nan, dtype='float32')
    wolne=np.ones((win.height, win.width), dtype=bool)      #piksele, ktore nie dostaly jeszcze wartosci

    for sciezka, _, r in w_bloku:
        with rasterio.open(sciezka) as src:
            mos, _tr=merge([src], bounds=win_bounds, res=native_cellsize, resampling=Resampling.nearest,
                           method='first', target_aligned_pixels=True)
        arr=np.full((win.height, win.width), np.nan, dtype='float32')
        h, w=min(win.height, mos.shape[1]), min(win.width, mos.shape[2])
        arr[:h, :w]=mos[0, :h, :w]
        ma=wolne & ~np.isnan(arr)
        blok[ma]=arr[ma]
        res[ma]=r
        wolne&=~ma
        if not wolne.any():
            break

    tr=from_origin(win_bounds[0], win_bounds[3], native_cellsize, native_cellsize)
    gmask=geometry_mask([geom_shape], out_shape=blok.shape, transform=tr, invert=True)
    return (np.where(gmask, blok, np.float32(np.nan)).astype('float32'),
            np.where(gmask, res, np.float32(np.nan)).astype('float32'))


def zbuduj_warstwe_bazowa(tablica, siatka, geometry, uzyte_kafle, indeks, blok_px=2048,
                          brudne=None, pelny_zapis=True, tablica_res=None):
    '''
    Buduje JEDNA warstwe czasowa (rok) poziomu bazowego BLOKAMI.

    tablica      : tablica zarr poziomu bazowego (time, y, x)
    uzyte_kafle  : lista sciezek, POSORTOWANA najnowsze->najstarsze
                   (np. sorted(tiffs, key=lambda p: find_date(p, None), reverse=True))
    indeks       : indeks czasu tej warstwy
    brudne       : lista prostokatow (left, bottom, right, top); jesli podana, przebudowane
                   zostana TYLKO bloki, ktore przecinaja ktorys z nich (aktualizacja przyrostowa)
    pelny_zapis  : True  -> blok bez zrodel tez jest zapisywany (jako NaN), zeby nadpisac stare
                            dane (przebudowa istniejacej warstwy)
                   False -> bloki bez zrodel sa pomijane (swieza, pusta warstwa - i tak NaN)
    tablica_res  : tablica zarr zmiennej rozdzielczosc_zrodla (time, y, x); jesli podana, dla kazdego piksela
                   zapisuje rozdzielczosc arkusza, z ktorego pochodzi wysokosc
    zwraca liczbe zapisanych blokow
    '''
    geom_shape=geometry if hasattr(geometry, 'bounds') else shape(geometry)
    zrodla=[]
    for p in uzyte_kafle:
        with rasterio.open(p) as src:
            zrodla.append((p, src.bounds, round(abs(src.res[0]), 4)))

    zapisane=0
    for y0, y1, x0, x1 in siatka.okna(blok_px):
        win=rasterio.windows.Window(x0, y0, x1 - x0, y1 - y0)
        win_bounds=siatka.okno_bounds(y0, y1, x0, x1)

        if brudne is not None and not any(przecina(win_bounds, b) for b in brudne):
            continue

        if geom_shape.intersects(box(*win_bounds)):
            mos_blok, res_blok=_zbuduj_blok_ze_zrodlem(zrodla, win_bounds, win, siatka.cellsize, geom_shape)
        else:
            mos_blok, res_blok=None, None

        if mos_blok is None:
            if pelny_zapis:
                pusty=np.full((y1 - y0, x1 - x0), np.nan, dtype='float32')
                tablica[indeks, y0:y1, x0:x1]=pusty
                if tablica_res is not None:
                    tablica_res[indeks, y0:y1, x0:x1]=pusty
                zapisane+=1
            continue

        #---ZAOKRAGLENIE WYSOKOSCI DO 2 MSC PO PRZECINKU (jak w konwerterach)---
        tablica[indeks, y0:y1, x0:x1]=np.round(mos_blok, 2).astype('float32')
        if tablica_res is not None:
            tablica_res[indeks, y0:y1, x0:x1]=res_blok
        zapisane+=1

    print(f'[NFP-ZARR] Warstwa bazowa (indeks czasu {indeks}): zapisano {zapisane} blokow')
    return zapisane
