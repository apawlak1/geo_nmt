# -*- coding: utf-8 -*-
'''
POZIOMY POCHODNE (OPCJONALNE): ZGRUBIENIE WARSTWY BAZOWEJ DO WIEKSZEGO PIKSELA

Poziom o faktorze f ma ten sam naroznik co baza i piksel f razy wiekszy, wiec kazdy jego
piksel = dokladnie f x f pikseli bazy. Liczony BLOKAMI z bazy (z zapasem na jadro
interpolacji), metoda z config.json: nearest / bilinear / cubic / average.
To zwykle zgrubienie - bez oceny jakosci (bez filtracji i bez MAE/RMSE).
'''

import warnings

import numpy as np
from rasterio.crs import CRS
from rasterio.enums import Resampling
from rasterio.transform import from_origin
from rasterio.warp import reproject

from zarr_magazyn import przecina

METODY_RESAMPLINGU = {'nearest': Resampling.nearest, 'bilinear': Resampling.bilinear,
                      'cubic': Resampling.cubic, 'average': Resampling.average}
ALIASY = {'bicubic': 'cubic'}


def nazwa_metody(metoda):
    m = ALIASY.get(str(metoda).strip().lower(), str(metoda).strip().lower())
    if m not in METODY_RESAMPLINGU:
        raise ValueError(f"nieznana metoda '{metoda}'. Dozwolone: {sorted(METODY_RESAMPLINGU)} (oraz 'bicubic')")
    return m


def _najgrubsza_w_komorkach(tab_res, indeks, by0, by1, bx0, bx1, faktor):
    '''NAJGRUBSZA rozdzielczosc zrodlowa (max) w kazdej komorce faktor x faktor pikseli bazy; NaN = brak danych'''
    a = np.asarray(tab_res[indeks, by0:by1, bx0:bx1], dtype='float32')
    h, w = (by1 - by0) // faktor, (bx1 - bx0) // faktor
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')          #komorki bez danych daja ostrzezenie 'All-NaN slice'
        return np.nanmax(a.reshape(h, faktor, w, faktor), axis=(1, 3)).astype('float32')


def przelicz_poziom(tab_baza, tab_poziom, siatka_baza, siatka_poziom, faktor, indeks,
                    blok_poziomu, metoda='bilinear', brudne=None, tab_baza_res=None, tab_poziom_res=None):
    '''
    tab_baza / tab_poziom : tablice zarr (time, y, x)
    blok_poziomu          : rozmiar bloku w pikselach POZIOMU (patrz Magazyn.blok_poziomu)
    brudne                : lista prostokatow (left, bottom, right, top); jesli podana, przeliczone
                            zostana TYLKO bloki przecinajace ktorys z nich (None = caly poziom)
    tab_baza_res / tab_poziom_res : tablice zmiennej rozdzielczosc_zrodla; na poziomie pochodnym komorka dostaje
                            NAJGRUBSZA rozdzielczosc zrodlowa sposrod pikseli bazy, ktore ja tworza
    '''
    resampling = METODY_RESAMPLINGU[nazwa_metody(metoda)]
    crs = CRS.from_epsg(siatka_baza.epsg)
    zapas = 2 * faktor + 2      #piksele bazy wokol bloku - wystarczy dla jadra cubic przy zgrubianiu
    zapisane = 0

    for ly0, ly1, lx0, lx1 in siatka_poziom.okna(blok_poziomu):
        okno_poz = siatka_poziom.okno_bounds(ly0, ly1, lx0, lx1)
        if brudne is not None and not any(przecina(okno_poz, b) for b in brudne):
            continue

        #---ZRODLOWE OKNO W BAZIE (z zapasem, przyciete do tablicy)---
        by0, by1 = max(0, ly0 * faktor - zapas), min(siatka_baza.height, ly1 * faktor + zapas)
        bx0, bx1 = max(0, lx0 * faktor - zapas), min(siatka_baza.width, lx1 * faktor + zapas)
        zrodlo = np.asarray(tab_baza[indeks, by0:by1, bx0:bx1], dtype='float32')

        cel = np.full((ly1 - ly0, lx1 - lx0), np.nan, dtype='float32')
        if tab_baza_res is not None and tab_poziom_res is not None:
            tab_poziom_res[indeks, ly0:ly1, lx0:lx1] = _najgrubsza_w_komorkach(
                tab_baza_res, indeks, ly0 * faktor, ly1 * faktor, lx0 * faktor, lx1 * faktor, faktor)
        if np.isnan(zrodlo).all():
            tab_poziom[indeks, ly0:ly1, lx0:lx1] = cel
            zapisane += 1
            continue

        cs = siatka_baza.cellsize
        src_tr = from_origin(siatka_baza.left + bx0 * cs, siatka_baza.top - by0 * cs, cs, cs)
        csp = siatka_poziom.cellsize
        dst_tr = from_origin(siatka_poziom.left + lx0 * csp, siatka_poziom.top - ly0 * csp, csp, csp)

        reproject(source=zrodlo, destination=cel, src_transform=src_tr, src_crs=crs,
                  dst_transform=dst_tr, dst_crs=crs, src_nodata=np.nan, dst_nodata=np.nan,
                  resampling=resampling)
        tab_poziom[indeks, ly0:ly1, lx0:lx1] = np.round(cel, 2)
        zapisane += 1

    print(f'[ZARR] Poziom {siatka_poziom.cellsize:g} m (indeks czasu {indeks}): zapisano {zapisane} blokow')
    return zapisane
