# Luma Bridge

Mostek między [Luma](https://github.com/iwanstudio/lg-luma) na telewizorze LG a Home Assistantem.

Encje wybiera się w Home Assistant. Telewizor dostaje tylko listę kafelków i może nacisnąć jeden z nich. Nie dostaje tokena do całego HA.

## Instalacja

1. W HACS: Integracje, menu, **Custom repositories**.
2. Adres: `https://github.com/iwanstudio/ha-luma-bridge`, kategoria **Integration**.
3. Pobierz **Luma Bridge** i zrestartuj Home Assistant.
4. Ustawienia, Urządzenia i usługi, **Dodaj integrację**, Luma Bridge.
5. Wybierz encje. Na następnym ekranie wpisz nazwę, która ma być na telewizorze. Może być taka sama jak na tablecie. Nazwa encji w Home Assistant się nie zmienia.
6. Na końcu HA pokaże kod, na przykład `a1b2-c3d4-e5f6-7890`.
7. Połączenie z telewizorem: w Lumie **Ustawienia → Home Assistant → Kod QR**. W HA: Luma Bridge, **Konfiguruj**, **Połącz telewizor kodem QR**. Otwórz stronę na telefonie i zeskanuj kod z telewizora. Adres i kod wpisują się same.
8. Ręczne wpisanie adresu i kodu w Lumie nadal działa.

Działa na lokalnym adresie, na przykład `http://192.168.3.10:8123`. Szyfrowany adres z zewnątrz nie jest potrzebny.

## Co robi naciśnięcie

| Encja | Naciśnięcie |
| --- | --- |
| światło, przełącznik, wiatrak, input_boolean, roleta | przełącza |
| scena, skrypt | uruchamia |
| przycisk | naciska |

## API

Nagłówek: `Authorization: Bearer KOD` albo `X-Luma-Token: KOD`.

Lista kafelków:

```http
GET /api/luma_bridge/tiles
```

```json
{
  "tiles": [
    {
      "entity_id": "light.kuchnia",
      "name": "Kuchnia",
      "domain": "light",
      "state": "on",
      "action": "toggle"
    }
  ]
}
```

Naciśnięcie kafelka, który jest na liście:

```http
POST /api/luma_bridge/press
```

```json
{"entity_id": "light.kuchnia"}
```

Innej encji telewizor nie uruchomi.
