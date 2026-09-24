# Pierwsze uruchomienie — instrukcja

## Działanie

`geo_nmt` automatyzuje pozyskiwanie **numerycznego modelu terenu (NMT)** z Geoportalu dla wybranego powiatu.
Zamiast ręcznie przeglądać skorowidze, pobierać pojedyncze arkusze i sklejać je w jedną całość, jedno
uruchomienie skryptu robi to wszystko za Ciebie. Zależnie od ustawień — oddaje gotowy wynik w jednej
z trzech postaci:

1. same pobrane, surowe pliki (bez przetwarzania),
2. pojedyncze pliki GeoTIFF (po jednym na arkusz), gotowe do wczytania w dowolnym programie GIS,
3. gotowy zbiór danych: albo **wieloczasową bazę Zarr** (wiele lat w jednym pliku, z możliwością
   dopisywania kolejnych lat później), albo **pojedynczą scaloną mozaikę GeoTIFF** na rok.

Program sam pobiera granice powiatu, dopasowuje układ współrzędnych, konwertuje formaty i scala arkusze.

---

## Krok 1: zainstaluj środowisko

Potrzebujesz Pythona wraz z kilkoma bibliotekami geoprzestrzennymi (m.in. `rasterio`, `geopandas`,
`xarray`, `zarr`). Najprościej i najpewniej zainstalować je przez **conda** (albo jej szybszy odpowiednik
`mamba`) — te biblioteki bywają kłopotliwe do zainstalowania samym `pip`.

### Jeśli nie masz jeszcze condy

Zainstaluj [Miniconda](https://docs.conda.io/en/latest/miniconda.html) (mały instalator, wystarczający do
tego zadania) — wybierz wersję dla swojego systemu (Windows/macOS/Linux) i przejdź przez instalator z
ustawieniami domyślnymi.

### Utworzenie środowiska

W repozytorium projektu jest plik `environment.yml`, który opisuje dokładnie, czego program potrzebuje.
Otwórz **Anaconda Prompt** (Windows) albo terminal (macOS/Linux), przejdź do folderu z projektem i
uruchom:

```bash
conda env create -f environment.yml
conda activate nmt-zarr
```

Pierwsze polecenie tworzy nowe, odizolowane środowisko o nazwie `nmt-zarr` i instaluje w nim wszystkie
potrzebne biblioteki — może potrwać kilka minut. Drugie polecenie je aktywuje; musisz je powtarzać przy
każdym kolejnym uruchomieniu terminala.

Żeby sprawdzić, czy instalacja się udała:

```bash
python -c "import rasterio, geopandas, zarr, xarray; print('OK')"
```

Jeśli zobaczysz `OK` bez błędów, środowisko jest gotowe.

---

## Krok 2: przygotuj plik `config.json`

Program czyta ustawienia z pliku `config.json`, który musi leżeć w tym samym folderze co plik
`geo_nmt.py`. W repozytorium jest już gotowy przykładowy `config.json` — otwórz go w dowolnym edytorze
tekstu i zmień pod siebie.

### Co zazwyczaj trzeba ustawić na start

| parametr | co to jest | przykład |
|---|---|---|
| `cache_dir` | folder na dysku, do którego trafią wszystkie pobrane i wygenerowane pliki. Zostanie utworzony, jeśli nie istnieje | `"C:/Users/Ty/Documents/nmt"` |
| `geo_nmt.powiat` | nazwa powiatu, dla którego mają zostać pobrane dane | `"Sopot"` |
| `geo_nmt.lata` | które lata pobrać — lista lat, tekst z latami po przecinku, albo `"auto"` (od pewnego roku do dziś) | `[2019, 2020, 2021]` albo `"auto"` |
| `geo_nmt.pobierz` | czy pobierać pliki bez pytania o potwierdzenie | `true` |
| `geo_nmt.zarr` | czy zbudować wieloczasową bazę Zarr | `true` |
| `geo_nmt.geotiff` | czy zamiast tego zapisać pojedynczą mozaikę GeoTIFF na rok | `true` (wyklucza się z `zarr`) |
| `geo_nmt.rozdzielczosc` | rozmiar piksela wyniku w metrach; puste (`null`) = dobierze się sam | `0.5` |

To jest tylko podstawowy, praktyczny minimum. Pełny opis wszystkich dostępnych parametrów i gotowe
przykłady konfiguracji (m.in. „tylko pobieranie”, „tylko GeoTIFF”, „pełna baza Zarr”) są w plikach
`README.md` i `INSTRUKCJA_OBSLUGI.md` w repozytorium — warto do nich zajrzeć, zanim zmienisz coś poza
tabelą powyżej.

**Jeśli nie jesteś pewna/pewien, co wpisać** — możesz zostawić `powiat` i `lata` puste (albo w ogóle je
pominąć): program zapyta o nie w konsoli przy uruchomieniu.

---

## Krok 3: uruchom program

W aktywowanym środowisku (`conda activate nmt-zarr`), w folderze z projektem:

```bash
python geo_nmt.py
```

Program zacznie od pobrania granic powiatu i skorowidza arkuszy, pokaże w konsoli, ile danych znalazł, a
następnie — zależnie od ustawień w configu — pobierze pliki i zbuduje wynik. Może to potrwać od kilku
minut do dłuższej chwili, zależnie od wielkości powiatu, liczby lat i szybkości serwera Geoportalu.

Po zakończeniu wszystkie wyniki znajdziesz w folderze podanym w `cache_dir`.

---

## Co dalej

- Żeby sprawdzić zawartość zbudowanej bazy Zarr, uruchom `python zarr_info.py`.
- Żeby uruchomić program ponownie później (np. po pojawieniu się nowych danych w Geoportalu), uruchom
  `python geo_nmt.py` jeszcze raz - program sam rozpozna, co już ma, i doda tylko brakujące dane.
