// Rollsportanlage: Hallendaten (BLU H&T) und Wetter (WS90)
// Holt die Daten jede Minute von /shelly-api/data.php und füllt
// <div id="hallen-box"> und <div id="wetter-box">.
// Einbinden mit: <script src="/js/shelly-wetter.js" defer></script>

(function () {
  var hallenBox = document.getElementById('hallen-box');
  var wetterBox = document.getElementById('wetter-box');
  if (!hallenBox && !wetterBox) return;

  var VERALTET_NACH_MIN = 30;
  var ZEIGE_GEFUEHLT = true;         // false = gefühlte Temperatur nie anzeigen
  var GEFUEHLT_AB_DIFFERENZ = 1;     // nur anzeigen, wenn sie mindestens so viele °C abweicht

  // Zahl im deutschen Format, z. B. 4,6
  function zahl(wert, stellen) {
    if (wert === null || wert === undefined || isNaN(wert)) return '–';
    return Number(wert).toLocaleString('de-DE', {
      minimumFractionDigits: stellen,
      maximumFractionDigits: stellen,
      useGrouping: false
    });
  }

  // "Stand: 12:45 Uhr" bzw. "Stand: 22.11. 18:00 Uhr", rot wenn veraltet
  function standZeile(iso, online) {
    if (!iso) return '';
    var d = new Date(iso);
    if (isNaN(d)) return '';
    var jetzt = new Date();
    var zeit = d.toLocaleTimeString('de-DE', { hour: '2-digit', minute: '2-digit' });
    var text = d.toDateString() === jetzt.toDateString()
      ? zeit + ' Uhr'
      : d.toLocaleDateString('de-DE', { day: '2-digit', month: '2-digit' }) + ' ' + zeit + ' Uhr';
    var veraltet = (jetzt - d) > VERALTET_NACH_MIN * 60 * 1000 || online === false;
    return '<div class="stand' + (veraltet ? ' veraltet' : '') + '">Stand: ' + text +
      (veraltet ? ' (keine aktuellen Daten)' : '') + '</div>';
  }

  // Kastenfarbe nach Temperatur
  var FARBKLASSEN = ['temp-rot', 'temp-gelb', 'temp-gruen', 'temp-blau', 'temp-lila'];
  function setzeTemperaturFarbe(box, temp) {
    var farbKlasse;
    if (temp > 30) {
      farbKlasse = 'temp-rot';
    } else if (temp >= 25) {
      farbKlasse = 'temp-gelb';
    } else if (temp >= 15) {
      farbKlasse = 'temp-gruen';
    } else if (temp >= 10) {
      farbKlasse = 'temp-blau';
    } else {
      farbKlasse = 'temp-lila';
    }
    FARBKLASSEN.forEach(function (k) { box.classList.remove(k); });
    box.classList.add(farbKlasse);
  }

  // ---------- Hallendaten (BLU H&T) ----------
  function zeigeHalle(h) {
    if (!h || h.temperatur === null || h.luftfeuchte === null) {
      hallenBox.textContent = 'Hallendaten aktuell nicht verfügbar';
      return;
    }
    var temp = h.temperatur;
    var feuchte = h.luftfeuchte;

    setzeTemperaturFarbe(hallenBox, temp);

    // Luftfeuchtigkeits-Kugel-Farbe
    var kugelKlasse;
    if (feuchte < 30) {
      kugelKlasse = 'kugel-rot';
    } else if (feuchte < 40) {
      kugelKlasse = 'kugel-gelb';
    } else if (feuchte < 60) {
      kugelKlasse = 'kugel-gruen';
    } else if (feuchte < 70) {
      kugelKlasse = 'kugel-gelb';
    } else if (feuchte < 80) {
      kugelKlasse = 'kugel-orange';
    } else {
      kugelKlasse = 'kugel-rot';
    }

    hallenBox.innerHTML =
      '<div class="box-titel">Aktuelle Hallendaten</div>' +
      '<div>Temperatur: <span class="wert">' + zahl(temp, 1) + ' °C</span></div>' +
      '<div>Luftfeuchtigkeit: <span class="wert">' + zahl(feuchte, 0) + ' %</span>' +
      '<span class="luft-kugel ' + kugelKlasse + '"></span></div>' +
      standZeile(h.stand, h.online);
  }

  // ---------- Icons (Dateien in /images/wetter/) ----------
  var ICON_PFAD = '/images/wetter/';

  function icon(name, klasse, text) {
    return '<img class="' + (klasse || 'icon') + '" src="' + ICON_PFAD + name + '.svg" alt="' + (text || '') + '"' +
      (text ? ' title="' + text + '"' : ' aria-hidden="true"') + '>';
  }

  // Art des Niederschlags grob nach Temperatur (der Sensor erkennt nur "nass")
  function niederschlag(w) {
    if (w.temperatur !== null && w.temperatur <= 0.5) return { icon: 'schnee', text: 'Schnee', aktiv: 'es schneit' };
    if (w.temperatur !== null && w.temperatur <= 2.5) return { icon: 'schneeregen', text: 'Schneeregen', aktiv: 'Schneeregen' };
    return { icon: 'regen', text: 'Regen', aktiv: 'es regnet' };
  }

  // Wetterlage aus Regensensor, Wind und Helligkeit abschätzen
  function wetterlage(w) {
    var lux = w.helligkeit_lux;
    if (w.regen_aktiv === true) return niederschlag(w);
    if (w.beaufort !== null && w.beaufort >= 6) return { icon: 'wind', text: 'windig' };
    if (lux === null) return null;
    if (lux < 20) return { icon: 'mond', text: 'Nacht' };
    if (lux >= 40000) return { icon: 'sonne', text: 'sonnig' };
    if (lux >= 15000) return { icon: 'sonne-wolke', text: 'leicht bewölkt' };
    return { icon: 'wolke', text: 'bewölkt' };
  }

  // ---------- Wetter (WS90) ----------
  var BEAUFORT_NAMEN = ['Windstille', 'leiser Zug', 'leichte Brise', 'schwache Brise', 'mäßige Brise',
    'frische Brise', 'starker Wind', 'steifer Wind', 'stürmischer Wind', 'Sturm', 'schwerer Sturm',
    'orkanartiger Sturm', 'Orkan'];
  // km/h-Bereich je Beaufort-Stufe (passend zu den Grenzen in data.php)
  var BEAUFORT_KMH = ['unter 1', '1–5', '6–11', '12–19', '20–28', '29–38', '39–49', '50–61',
    '62–74', '75–88', '89–102', '103–117', 'ab 118'];

  // Zuletzt angezeigter Winkel des Pfeils (fortlaufend, kann über 360 hinausgehen),
  // damit er bei jeder Aktualisierung von dort aus zur neuen Richtung dreht
  var letzterWinkel = null;

  function begrenzt(wert, min, max) { return Math.min(max, Math.max(min, wert)); }

  // Windrose: Pfeil kommt aus der Windrichtung und zeigt, wohin der Wind weht.
  // Der Zeiger dreht beim Laden aus der alten Richtung in die neue (kürzester Weg),
  // der Pfeil darin pendelt leicht: stärker bei Böen, schneller bei mehr Wind.
  function windrose(w) {
    var html = '<span class="windrose" role="img" aria-label="Windrose">' + icon('windrose', 'rose-scheibe');
    if (w.beaufort > 0 && w.wind_richtung_grad !== null) {
      var start = letzterWinkel === null ? 0 : letzterWinkel;
      var ziel = start + ((((Number(w.wind_richtung_grad) - start) % 360) + 540) % 360) - 180;
      letzterWinkel = ziel;
      var boeenPlus = w.boeen_kmh !== null ? Math.max(0, w.boeen_kmh - w.wind_kmh) : 0;
      var schwank = begrenzt(2 + boeenPlus / 2, 2, 12);
      var dauer = begrenzt(4 - w.wind_kmh / 15, 1.2, 4);
      html += '<span class="rose-zeiger" style="transform: rotate(' + start + 'deg)" data-ziel="' + ziel + '">' +
        '<img class="rose-pfeil" src="' + ICON_PFAD + 'windrose-pfeil.svg" alt="" aria-hidden="true" ' +
        'style="--schwank: ' + schwank.toFixed(1) + 'deg; --dauer: ' + dauer.toFixed(1) + 's"></span>';
    } else {
      html += icon('windrose-still', 'rose-pfeil');
    }
    return html + '</span>';
  }

  function windBlock(w) {
    if (w.wind_kmh === null) return '';
    var zeilen = [];
    if (w.beaufort === 0) {
      zeilen.push('<span class="wert">windstill</span>');
    } else {
      zeilen.push('<span class="wert">' + zahl(w.wind_kmh, 0) + '&nbsp;km/h</span> aus ' + w.wind_richtung);
      if (w.boeen_kmh !== null && w.boeen_kmh >= 10) {  // kleine Böen nicht erwähnen
        zeilen.push('Böen bis ' + zahl(w.boeen_kmh, 0) + '&nbsp;km/h');
      }
      zeilen.push('<span class="nebenwert">' + BEAUFORT_NAMEN[w.beaufort] +
        ' (' + BEAUFORT_KMH[w.beaufort] + '&nbsp;km/h)</span>');
    }
    return '<div class="wind-block">' + windrose(w) +
      '<div class="wind-details"><div>' + icon('wind') + 'Wind</div>' +
      zeilen.map(function (z) { return '<div>' + z + '</div>'; }).join('') +
      '</div></div>';
  }

  function regenText(w) {
    var teile = [];
    if (w.regen_aktiv === true) teile.push('<span class="wert">' + niederschlag(w).aktiv + '</span>');
    else if (w.regen_aktiv === false) teile.push('trocken');
    if (w.regen_heute_mm !== null) teile.push('heute ' + zahl(w.regen_heute_mm, 1) + '&nbsp;mm');
    return teile.length ? teile.join(' · ') : '–';
  }

  function zeigeWetter(w) {
    if (!w || w.temperatur === null) {
      wetterBox.textContent = 'Wetterdaten aktuell nicht verfügbar';
      return;
    }

    var gefuehlt = '';
    if (ZEIGE_GEFUEHLT && w.gefuehlt !== null &&
        Math.abs(w.gefuehlt - w.temperatur) >= GEFUEHLT_AB_DIFFERENZ) {
      gefuehlt = '<span class="gefuehlt">(gefühlt ' + zahl(w.gefuehlt, 1) + '&nbsp;°C)</span>';
    }
    var lage = wetterlage(w);
    var unterzeile = [];
    if (lage) unterzeile.push(lage.text);
    if (w.temp_min_heute !== null && w.temp_max_heute !== null) {
      unterzeile.push('heute ' + zahl(w.temp_min_heute, 1) + ' bis ' + zahl(w.temp_max_heute, 1) + '&nbsp;°C');
    }
    var minMax = unterzeile.length
      ? '<div class="nebenwert">' + unterzeile.join(' · ').replace(/^./, function (c) { return c.toUpperCase(); }) + '</div>'
      : '';

    var zeilen = [
      ['niederschlag', 'Niederschlag', regenText(w)],
      ['tropfen', 'Luftfeuchte', '<span class="wert">' + zahl(w.luftfeuchte, 0) + '&nbsp;%</span>' +
        (w.taupunkt !== null ? ' · Taupunkt ' + zahl(w.taupunkt, 1) + '&nbsp;°C' : '')],
      ['luftdruck', 'Luftdruck', w.luftdruck !== null ? '<span class="wert">' + zahl(w.luftdruck, 0) + '&nbsp;hPa</span>' : '–'],
      ['uv', 'UV-Index', w.uv_index !== null
        ? '<span class="wert">' + zahl(w.uv_index, 0) + '</span> (' + w.uv_stufe + ')' : '–']
    ];

    wetterBox.innerHTML =
      '<div class="box-titel">Aktuelles Wetter beim KREC</div>' +
      '<div class="temp-gross">' +
        (lage ? icon(lage.icon, 'icon-gross', lage.text) : '') +
        '<span class="wert">' + zahl(w.temperatur, 1) + '&nbsp;°C</span>' + gefuehlt + '</div>' +
      minMax +
      windBlock(w) +
      '<dl>' + zeilen.map(function (z) {
        return '<dt>' + icon(z[0]) + z[1] + '</dt><dd>' + z[2] + '</dd>';
      }).join('') + '</dl>' +
      standZeile(w.stand, w.online);

    // Zeiger erst nach dem Einfügen auf den Zielwinkel setzen, damit die CSS-Transition greift
    var zeiger = wetterBox.querySelector('.rose-zeiger');
    if (zeiger) {
      void zeiger.offsetWidth;
      zeiger.style.transform = 'rotate(' + zeiger.getAttribute('data-ziel') + 'deg)';
    }
  }

  function laden() {
    fetch('/shelly-api/data.php', { cache: 'no-store' })
      .then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      })
      .then(function (daten) {
        if (hallenBox) zeigeHalle(daten.halle);
        if (wetterBox) zeigeWetter(daten.wetter);
      })
      .catch(function (err) {
        if (hallenBox) hallenBox.textContent = 'Hallendaten aktuell nicht verfügbar';
        if (wetterBox) wetterBox.textContent = 'Wetterdaten aktuell nicht verfügbar';
        console.error('Shelly-Daten:', err);
      });
  }

  laden();
  setInterval(laden, 60 * 1000); // jede Minute aktualisieren
})();
