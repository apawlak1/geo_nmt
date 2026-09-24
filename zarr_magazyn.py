# -*- coding: utf-8 -*-
'''
STORE ZARR: SIATKA, STRUKTURA, DOPISYWANIE WARSTW (LAT), MANIFEST

Kazda rozdzielczosc to OSOBNY store .zarr, a w nim - w KORZENIU - zmienna nmt (time, y, x)
z wieloma latami w jednym rastrze (wymiar time). Tak wymaga tego ArcGIS Pro (folder z
rozszerzeniem .zarr i plikiem .zgroup, zmienne w korzeniu) i tak zalecaja Esri: rozne
rozdzielczosci przestrzenne w osobnych zbiorach.

    Sopot_WIELOCZASOWA.zarr/          <- poziom bazowy (np. 0.5 m)
        .zgroup  .zattrs  nmt/  rozdzielczosc_zrodla/  x/  y/  time/  time_bnds/  spatial_ref/
    Sopot_WIELOCZASOWA_opis.json      <- opis store'u i manifest lat (OBOK folderu .zarr, nie w srodku)
    Sopot_WIELOCZASOWA_5m.zarr/       <- poziomy pochodne (opcjonalne, z config.json)
    Sopot_WIELOCZASOWA_5m_opis.json
    Sopot_WIELOCZASOWA_10m.zarr/

Wszystkie stores dziela ta sama siatke, ZAMROZONA przy tworzeniu: poziom o faktorze f ma ten sam
lewy gorny naroznik i piksel f razy wiekszy, wiec kazdy jego piksel = dokladnie f x f pikseli bazy.

Zmienne: nmt = wysokosc [m]; rozdzielczosc_zrodla = rozdzielczosc arkusza [m], z ktorego pochodzi wysokosc w
danym pikselu i roku (0.5 = prawdziwy szczegol, 5 = piksel rozciagniety z arkusza 5 m). Na poziomach pochodnych
to NAJGRUBSZA rozdzielczosc zrodlowa w komorce. Obie zmienne maja te sama siatke i wymiar time.
Czas: rok = PRZEDZIAL (time_bnds od 1 stycznia do 31 grudnia), wartosc time = 1 lipca; jednostki
'seconds since 1970-01-01'. Dzieki temu warstwa obowiazuje przez caly rok, a nie tylko w jednym dniu.
Metadane (wspolrzedne, CRS, czas) zapisuje xarray/rioxarray wg konwencji CF - to czytaja GDAL, QGIS
i ArcGIS. Same DANE zapisujemy bezposrednio przez zarr (przypisanie do wycinka tablicy).

Manifest NIE moze lezec w atrybutach korzenia: xarray zeruje je przy kazdym dopisaniu roku.
Dlatego jest w pliku *_opis.json OBOK store'u (zapis atomowy) - obcy plik w srodku hierarchii Zarr
wywoluje ostrzezenia zarr i moze przeszkadzac innym czytnikom (ArcGIS). Zapisu danych nie chroni transakcja, wiec warstwa ma
status: 'w_toku' (przed zapisem) -> 'kompletna' (po zapisie); 'w_toku' jest budowana od nowa.
'''

import json
import warnings
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path

import dask.array as da
import numpy as np
import pandas as pd
import rioxarray  # noqa: F401 - rejestruje akcesor .rio na xarray
import xarray as xr
import zarr
from rasterio.transform import from_origin

_ZARR3 = int(zarr.__version__.split('.')[0]) >= 3
WERSJA_FORMATU = 1
NAZWA_ZMIENNEJ = 'nmt'
ZMIENNA_ROZDZIELCZOSCI = 'rozdzielczosc_zrodla'


class BladMagazynu(RuntimeError):
    pass


# ----------------------------------------------------------------------
# SIATKA
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class Siatka:
    left: float
    top: float
    cellsize: float
    width: int
    height: int
    epsg: int = 2180

    @property
    def transform(self):
        return from_origin(self.left, self.top, self.cellsize, self.cellsize)

    @property
    def bounds(self):
        #(left, bottom, right, top)
        return (self.left, self.top - self.height * self.cellsize,
                self.left + self.width * self.cellsize, self.top)

    def xs(self):
        return self.left + (np.arange(self.width) + 0.5) * self.cellsize

    def ys(self):
        return self.top - (np.arange(self.height) + 0.5) * self.cellsize

    def poziom(self, faktor):
        if self.width % faktor or self.height % faktor:
            raise ValueError(f'wymiary siatki {self.width}x{self.height} nie dziela sie przez {faktor}')
        return Siatka(self.left, self.top, round(self.cellsize * faktor, 6),
                      self.width // faktor, self.height // faktor, self.epsg)

    def okno_bounds(self, y0, y1, x0, x1):
        #granice (left, bottom, right, top) okna w pikselach
        return (self.left + x0 * self.cellsize, self.top - y1 * self.cellsize,
                self.left + x1 * self.cellsize, self.top - y0 * self.cellsize)

    def okna(self, blok_px):
        #---OKNA (y0, y1, x0, x1) O ROZMIARZE blok_px, WYROWNANE DO CHUNKOW---
        for y0 in range(0, self.height, blok_px):
            for x0 in range(0, self.width, blok_px):
                yield y0, min(y0 + blok_px, self.height), x0, min(x0 + blok_px, self.width)

    def do_slownika(self):
        return asdict(self)

    @staticmethod
    def ze_slownika(d):
        return Siatka(float(d['left']), float(d['top']), float(d['cellsize']),
                      int(d['width']), int(d['height']), int(d.get('epsg', 2180)))


def nazwa_grupy(cellsize):
    #np. 0.5 -> '0.5m', 5.0 -> '5m'
    return f'{cellsize:g}m'


def przecina(a, b):
    #czy dwa prostokaty (left, bottom, right, top) maja niepusty przekroj
    return not (a[2] <= b[0] or a[0] >= b[2] or a[3] <= b[1] or a[1] >= b[3])


def waliduj_poziomy(bazowa, poziomy):
    '''
    KAZDY poziom musi byc CALKOWITA wielokrotnoscia rozdzielczosci bazowej (>= 2x),
    inaczej siatki poziomow nie nakladalyby sie na siatke bazowa.
    Zwraca liste faktorow (int) w tej samej kolejnosci.
    '''
    faktory = []
    for p in poziomy:
        f = p / bazowa
        if abs(f - round(f)) > 1e-6 or round(f) < 2:
            raise ValueError(f'poziom {p} m nie jest calkowita wielokrotnoscia (>= 2x) '
                             f'rozdzielczosci bazowej {bazowa} m')
        faktory.append(int(round(f)))
    if len(set(faktory)) != len(faktory):
        raise ValueError('poziomy w konfiguracji sie powtarzaja')
    return faktory


# ----------------------------------------------------------------------
# JEDEN STORE (.zarr) Z ZMIENNA nmt W KORZENIU
# ----------------------------------------------------------------------
PLIK_OPISU = 'nmtzarr.json'


def _znacznik_czasu(rok):
    #wartosc czasu warstwy = SRODEK roku (1 lipca); zakres roku jest w time_bnds
    return pd.Timestamp(year=int(rok), month=7, day=1)


def _granice_roku(rok):
    #warstwa obowiazuje przez CALY rok: od 1 stycznia do 31 grudnia (konwencja CF: time_bnds)
    return pd.Timestamp(int(rok), 1, 1), pd.Timestamp(int(rok), 12, 31, 23, 59, 59)


#kodowanie czasu wspolne dla time i time_bnds (CF wymaga tych samych jednostek); epoka 1970 jest najpowszechniejsza
KODOWANIE_CZASU = {'units': 'seconds since 1970-01-01 00:00:00', 'calendar': 'standard', 'dtype': 'int64'}


def _teraz():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


SUFIKS_OPISU = '_opis.json'


def _rdzen(sciezka_store):
    p = Path(sciezka_store)
    return p.name[:-5] if p.name.endswith('.zarr') else p.name


def plik_opisu(sciezka_store):
    #Sopot_WIELOCZASOWA.zarr -> Sopot_WIELOCZASOWA_opis.json (w tym samym folderze co store)
    p = Path(sciezka_store)
    return p.with_name(f'{_rdzen(p)}{SUFIKS_OPISU}')


def migruj_opis(sciezka_store):
    #starsze wersje trzymaly opis W SRODKU store'u (nmtzarr.json) albo obok jako <store>.nmtzarr.json -
    #przenosze go pod obecna nazwe (dane .zarr bez zmian)
    p = Path(sciezka_store)
    nowy = plik_opisu(p)
    for stary in (p / PLIK_OPISU, p.with_name(f'{_rdzen(p)}.nmtzarr.json')):
        if stary.exists():
            if nowy.exists():
                stary.unlink()
            else:
                stary.replace(nowy)


def _zapisz_json(sciezka, dane):
    #zapis atomowy: przerwany zapis nie zostawi uszkodzonego pliku
    sciezka = Path(sciezka)
    tmp = sciezka.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(dane, ensure_ascii=False, indent=1), encoding='utf-8')
    tmp.replace(sciezka)


def _czytaj_json(sciezka):
    return json.loads(Path(sciezka).read_text(encoding='utf-8'))


def _atrybuty_cf(ds, tytul):
    #---UZUPELNIENIE METADANYCH CF (rioxarray zapisuje CRS i transform, ale nie os ani jednostek)---
    #ds: Dataset ze zmiennymi nmt i rozdzielczosc_zrodla (wspolne wspolrzedne x, y, time)
    ds[NAZWA_ZMIENNEJ].attrs.update({'long_name': 'numeryczny model terenu', 'units': 'm'})
    ds[ZMIENNA_ROZDZIELCZOSCI].attrs.update({'long_name': 'rozdzielczosc arkusza zrodlowego', 'units': 'm'})
    ds.coords['x'].attrs.update({'axis': 'X', 'standard_name': 'projection_x_coordinate',
                                 'long_name': 'x coordinate of projection', 'units': 'm'})
    ds.coords['y'].attrs.update({'axis': 'Y', 'standard_name': 'projection_y_coordinate',
                                 'long_name': 'y coordinate of projection', 'units': 'm'})
    ds.coords['time'].attrs.update({'axis': 'T', 'standard_name': 'time', 'long_name': 'time'})
    ds.attrs.update({'Conventions': 'CF-1.8', 'title': tytul})
    return ds


class _Store:
    def __init__(self, sciezka, zarr_format=2):
        self.sciezka = Path(sciezka)
        self.zarr_format = zarr_format

    def istnieje(self):
        return (self.sciezka / '.zgroup').exists() or (self.sciezka / 'zarr.json').exists()

    def _kw(self):
        return {'zarr_format': self.zarr_format} if (_ZARR3 and self.zarr_format) else {}

    def lata(self):
        if not self.istnieje():
            return None
        try:
            ds = xr.open_zarr(str(self.sciezka), consolidated=False)
        except Exception:
            return None
        return [int(pd.Timestamp(t).year) for t in ds['time'].values]

    def _szkielet(self, siatka, rok, blok, tytul):
        #---PUSTY (NaN) LENIWY DataArray Z PRAWDZIWYMI WSPOLRZEDNYMI, CRS I CZASEM---
        bl = min(blok, siatka.height, siatka.width)

        def pusta(nazwa):
            dane = da.full((1, siatka.height, siatka.width), np.nan, chunks=(1, bl, bl), dtype='float32')
            x = xr.DataArray(dane, dims=('time', 'y', 'x'), name=nazwa,
                             coords={'time': pd.DatetimeIndex([_znacznik_czasu(rok)]),
                                     'y': siatka.ys(), 'x': siatka.xs()})
            x.rio.write_crs(f'EPSG:{siatka.epsg}', inplace=True)
            x.rio.write_transform(siatka.transform, inplace=True)
            #encoded=True: _FillValue trafia do encodingu (inaczej dopisywanie lat sie wywala)
            x.rio.write_nodata(np.nan, encoded=True, inplace=True)
            return x

        #---DWIE ZMIENNE NA TEJ SAMEJ SIATCE I OSI CZASU: nmt oraz rozdzielczosc arkusza zrodlowego---
        ds = xr.Dataset({NAZWA_ZMIENNEJ: pusta(NAZWA_ZMIENNEJ),
                         ZMIENNA_ROZDZIELCZOSCI: pusta(ZMIENNA_ROZDZIELCZOSCI)})
        ds = _atrybuty_cf(ds, tytul)
        #---ROK JAKO PRZEDZIAL: time_bnds = [1 stycznia, 31 grudnia]; time = srodek roku---
        pocz, kon = _granice_roku(rok)
        ds['time_bnds'] = (('time', 'bnds'), np.array([[pocz, kon]], dtype='datetime64[ns]'))
        ds['time'].attrs['bounds'] = 'time_bnds'
        return ds

    def dopisz_rok(self, siatka, rok, blok, tytul=''):
        '''tworzy store (pierwszy rok) albo dopisuje rok jako nowy plaster time; idempotentne. Zwraca indeks.'''
        lata = self.lata()
        ds = self._szkielet(siatka, rok, blok, tytul)
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            if lata is None:
                self.sciezka.parent.mkdir(parents=True, exist_ok=True)
                ds.to_zarr(str(self.sciezka), mode='a', compute=False, consolidated=False,
                           encoding={'time': dict(KODOWANIE_CZASU), 'time_bnds': dict(KODOWANIE_CZASU)},
                           **self._kw())
                return 0
            if not (self.sciezka / 'time_bnds').exists():
                raise BladMagazynu(
                    f"{self.sciezka.name} ma stary zapis czasu (rok jako pojedynczy dzien, bez przedzialu). "
                    f"Usun store razem z plikami *_opis.json i zbuduj go od nowa (pobrane pliki zostaja w cache).")
            if not (self.sciezka / ZMIENNA_ROZDZIELCZOSCI).exists():
                raise BladMagazynu(
                    f"{self.sciezka.name} nie ma zmiennej '{ZMIENNA_ROZDZIELCZOSCI}' (store z poprzedniej wersji). "
                    f"Usun store razem z plikami *_opis.json i zbuduj go od nowa (pobrane pliki zostaja w cache).")
            if int(rok) in lata:
                return lata.index(int(rok))
            if lata and int(rok) < max(lata):
                print(f'[UWAGA] Rok {rok} jest starszy niz ostatnia warstwa ({max(lata)}) - trafi na '
                      f'koniec osi czasu (przy odczycie uzyj .sortby("time")).')
            ds.to_zarr(str(self.sciezka), mode='a', append_dim='time', compute=False,
                       consolidated=False, **self._kw())
            return len(lata)

    def tablica(self, zmienna=NAZWA_ZMIENNEJ):
        return zarr.open_group(str(self.sciezka), mode='r+')[zmienna]

    def czytaj_warstwe(self, rok, zmienna=NAZWA_ZMIENNEJ):
        ds = xr.open_zarr(str(self.sciezka), consolidated=False, decode_coords='all')
        indeksy = np.where(ds['time'].dt.year.values == int(rok))[0]
        if not len(indeksy):
            raise KeyError(f'brak warstwy dla roku {rok} w {self.sciezka.name}')
        return ds[zmienna].isel(time=int(indeksy[0]))


# ----------------------------------------------------------------------
# ZESTAW STORE'OW (BAZA + POZIOMY) Z JEDNYM MANIFESTEM
# ----------------------------------------------------------------------
class Magazyn:
    def __init__(self, sciezka, zarr_format=2, blok_px=2048):
        self.sciezka = Path(sciezka)          #store bazowy
        self.zarr_format = zarr_format
        self.blok_px = int(blok_px)

    # ---- opis i manifest (plik *_opis.json obok store'u bazowego) ----
    def _plik(self):
        return plik_opisu(self.sciezka)

    def _dane(self):
        return _czytaj_json(self._plik())

    def istnieje(self):
        migruj_opis(self.sciezka)
        if not self._plik().exists():
            return False
        if not self.sciezka.exists():
            #plik opisu zostal, a folder .zarr usunieto (np. recznie): manifest bez danych jest bledny -
            #inaczej skrypt uznalby lata za 'aktualne' i nie zbudowal ich ponownie
            self._usun_osierocone_opisy()
            return False
        return True

    def _usun_osierocone_opisy(self):
        osierocone = [self._plik()] + [
            p for p in self.sciezka.parent.glob(f'{_rdzen(self.sciezka)}_*{SUFIKS_OPISU}')
            if not p.with_name(p.name[:-len(SUFIKS_OPISU)] + '.zarr').exists()]
        for p in osierocone:
            p.unlink(missing_ok=True)
        print(f"[UWAGA] Znaleziono plik opisu bez folderu store'u ({self.sciezka.name}) - usunieto "
              f"nieaktualny manifest, lata zostana zbudowane od nowa.")

    def _sprawdz_stary_format(self):
        #store z poprzedniej wersji: grupy 0.5m/5m/... w jednym .zarr, manifest w atrybutach korzenia
        if not self.istnieje() and ((self.sciezka / '.zgroup').exists() or (self.sciezka / 'zarr.json').exists()):
            raise BladMagazynu(
                f"{self.sciezka} to store w STARYM formacie (poziomy jako grupy w jednym .zarr; ArcGIS go nie "
                f"czyta). Usun ten folder i uruchom skrypt ponownie - pobrane pliki i skonwertowane GeoTIFF-y "
                f"zostaja w cache, wiec odbudowa jest szybka.")

    def utworz(self, siatka, faktory_poziomow, meta=None):
        '''zapisuje opis store'u i ZAMRAZA siatke oraz liste poziomow'''
        self._sprawdz_stary_format()
        if self.istnieje():
            raise BladMagazynu(f'store juz istnieje: {self.sciezka}')
        self.sciezka.mkdir(parents=True, exist_ok=True)
        opis = {'wersja_formatu': WERSJA_FORMATU, 'rola': 'baza',
                'siatka': siatka.do_slownika(),
                'faktory_poziomow': [int(f) for f in faktory_poziomow],
                'utworzono': _teraz()}
        opis.update(meta or {})
        _zapisz_json(self._plik(), {'nmtzarr': opis, 'warstwy': {}})
        print(f'[ZARR] Utworzono store: {self.sciezka} '
              f'(siatka {siatka.width}x{siatka.height} px, piksel {siatka.cellsize:g} m)')

    def opis(self):
        if not self.istnieje():
            self._sprawdz_stary_format()
            raise BladMagazynu(f'to nie jest store z tego narzedzia (albo nie istnieje): {self.sciezka}')
        return dict(self._dane()['nmtzarr'])

    def siatka(self):
        return Siatka.ze_slownika(self.opis()['siatka'])

    def faktory(self):
        return list(self.opis()['faktory_poziomow'])

    def sciezka_grupy(self, nazwa):
        '''sciezka store'u dla rozdzielczosci o danej nazwie (baza = store bazowy, poziomy = obok, z sufiksem)'''
        if nazwa == nazwa_grupy(self.siatka().cellsize):
            return self.sciezka
        rdzen = self.sciezka.name[:-5] if self.sciezka.name.endswith('.zarr') else self.sciezka.name
        return self.sciezka.with_name(f'{rdzen}_{nazwa}.zarr')

    def grupy(self):
        '''lista (nazwa, faktor, siatka) - baza (faktor 1) + poziomy pochodne; nazwa to np. 0.5m albo 5m'''
        baza = self.siatka()
        wynik = [(nazwa_grupy(baza.cellsize), 1, baza)]
        for f in self.faktory():
            s = baza.poziom(f)
            wynik.append((nazwa_grupy(s.cellsize), f, s))
        return wynik

    def manifest(self):
        if not self.istnieje():
            return {}
        return {int(k): v for k, v in self._dane().get('warstwy', {}).items()}

    def zapisz_rekord(self, rok, rekord):
        dane = self._dane()
        dane.setdefault('warstwy', {})[str(int(rok))] = rekord
        _zapisz_json(self._plik(), dane)

    def rekord_nowy(self, indeks, arkusze, status, **dodatkowe):
        rec = {'indeks': int(indeks), 'status': status, 'arkusze': arkusze, 'zmieniono': _teraz()}
        rec.update(dodatkowe)
        return rec

    # ---- warstwy czasowe ----
    def blok_poziomu(self, faktor):
        #---ROZMIAR BLOKU (i chunku) W PIKSELACH POZIOMU: zrodlo w bazie ~ blok_px---
        if faktor == 1:
            return self.blok_px
        return max(128, self.blok_px // faktor)

    def przygotuj_warstwe(self, rok):
        '''
        zapewnia miejsce na rok we WSZYSTKICH stores (baza + poziomy); idempotentne:
        jesli rok juz jest, nic nie dopisuje. Zwraca indeks czasu.
        '''
        opis = self.opis()
        indeks = None
        for nazwa, faktor, siatka in self.grupy():
            store = _Store(self.sciezka_grupy(nazwa), self.zarr_format)
            tytul = f"NMT {opis.get('powiat', '')} - piksel {siatka.cellsize:g} m - warstwy roczne".replace('  ', ' ')
            idx = store.dopisz_rok(siatka, rok, self.blok_poziomu(faktor), tytul)
            migruj_opis(store.sciezka)
            if faktor != 1 and not plik_opisu(store.sciezka).exists():
                _zapisz_json(plik_opisu(store.sciezka), {'nmtzarr': {
                    'wersja_formatu': WERSJA_FORMATU, 'rola': 'poziom', 'faktor': faktor,
                    'baza': self.sciezka.name, 'siatka': siatka.do_slownika()}})
            if indeks is None:
                indeks = idx
            elif indeks != idx:
                raise BladMagazynu(f'niespojne indeksy czasu miedzy stores dla roku {rok} '
                                   f'({indeks} vs {idx} w {store.sciezka.name}) - store uszkodzony?')
        return indeks

    def tablica(self, nazwa, zmienna=NAZWA_ZMIENNEJ):
        return _Store(self.sciezka_grupy(nazwa), self.zarr_format).tablica(zmienna)

    def czytaj_warstwe(self, nazwa, rok, zmienna=NAZWA_ZMIENNEJ):
        return _Store(self.sciezka_grupy(nazwa), self.zarr_format).czytaj_warstwe(rok, zmienna)
