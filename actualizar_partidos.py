import asyncio
from datetime import datetime, timedelta, timezone
import requests
import unicodedata

# URL base exacta de tu Realtime Database en Firebase
FIREBASE_BASE_URL = "https://plus9-1d005-default-rtdb.firebaseio.com"

# Zona horaria de Honduras (UTC-6)
HONDURAS_TZ = timezone(timedelta(hours=-6))

def normalizar_texto(texto):
    """Elimina acentos y pasa a minúsculas para comparar nombres."""
    if not texto:
        return ""
    return ''.join(
        c for c in unicodedata.normalize('NFD', texto.lower())
        if unicodedata.category(c) != 'Mn'
    )

def calcular_puntaje_coincidencia(local_fb, visitante_fb, home_espn, away_espn):
    """Calcula un puntaje de coincidencia robusto para evitar falsos positivos."""
    stopwords = {"fc", "cf", "club", "de", "del", "la", "el", "los", "las", "cd", "sc", "sad"}
    
    tokens_l_fb = set(local_fb.split()) - stopwords
    tokens_v_fb = set(visitante_fb.split()) - stopwords
    tokens_l_espn = set(home_espn.split()) - stopwords
    tokens_v_espn = set(away_espn.split()) - stopwords
    
    tokens_l_fb = tokens_l_fb or set(local_fb.split())
    tokens_v_fb = tokens_v_fb or set(visitante_fb.split())
    tokens_l_espn = tokens_l_espn or set(home_espn.split())
    tokens_v_espn = tokens_v_espn or set(away_espn.split())

    inter_normal_l = tokens_l_fb.intersection(tokens_l_espn)
    inter_normal_v = tokens_v_fb.intersection(tokens_v_espn)
    
    inter_inv_l = tokens_l_fb.intersection(tokens_v_espn)
    inter_inv_v = tokens_v_fb.intersection(tokens_l_espn)
    
    score_normal = 0
    if inter_normal_l and inter_normal_v:
        score_normal = len(inter_normal_l) + len(inter_normal_v) + (50 if local_fb in home_espn or home_espn in local_fb else 0)
        
    score_invertido = 0
    if inter_inv_l and inter_inv_v:
        score_invertido = len(inter_inv_l) + len(inter_inv_v) + (50 if local_fb in away_espn or away_espn in local_fb else 0)
        
    if score_normal >= score_invertido:
        return score_normal, False
    else:
        return score_invertido, True

def es_deporte_futbol(torneo):
    """Determina si un evento corresponde a fútbol para aplicar la API de ESPN."""
    t = normalizar_texto(torneo)
    palabras_clave_no_futbol = ["ufc", "boxeo", "boxing", "nba", "basketball", "mlb", "beisbol", "formula", "f1", "tennis", "tenis", "nfl", "wnba", "hockey"]
    for palabra in palabras_clave_no_futbol:
        if palabra in t:
            return False
    return True

async def actualizar_marcador_rest(partido_id, payload):
    """Actualiza los datos del evento en Firebase usando la API REST."""
    url = f"{FIREBASE_BASE_URL}/agenda_deportiva/{partido_id}.json"
    try:
        response = requests.patch(url, json=payload, timeout=10)
        if response.status_code == 200:
            print(f"  ✅ Actualizado [{partido_id}]: {payload.get('estado')} ({payload.get('tiempo', '')}) - {payload.get('golesLocal', '')}:{payload.get('golesVisitante', '')}")
        else:
            print(f"  ❌ Error al actualizar Firebase REST: {response.status_code} - {response.text}")
    except Exception as e:
        print(f"  ❌ Error de conexión con Firebase: {e}")

def obtener_partidos_espn(fecha_str):
    """Descarga todos los marcadores de fútbol del día desde la API pública de ESPN."""
    try:
        fecha_espn = fecha_str.replace("-", "")
        url = f"https://site.api.espn.com/apis/site/v2/sports/soccer/all/scoreboard?dates={fecha_espn}&limit=300"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        response = requests.get(url, headers=headers, timeout=15)
        if response.status_code == 200:
            return response.json().get("events", [])
    except Exception as e:
        print(f"  ❌ Error al conectar con la API de ESPN: {e}")
    return []

async def sincronizar_deportes_ciclo():
    ahora = datetime.now(HONDURAS_TZ)
    fecha_hoy = ahora.strftime('%Y-%m-%d')
    fecha_mañana = (ahora + timedelta(days=1)).strftime('%Y-%m-%d')
    print(f"\n 🔄 Sincronizando agenda deportiva ({ahora.strftime('%H:%M:%S')} CST)...")

    hay_partidos_en_vivo = False

    try:
        response = requests.get(f"{FIREBASE_BASE_URL}/agenda_deportiva.json", timeout=15)
        if response.status_code != 200 or not response.json():
            print(f" ⚠️ No hay eventos registrados en la agenda.")
            return False
        partidos = response.json()
    except Exception as e:
        print(f" ❌ Error al leer Firebase: {e}")
        return False

    eventos_raw = obtener_partidos_espn(fecha_hoy) + obtener_partidos_espn(fecha_mañana)
    eventos_dict = {}
    for ev in eventos_raw:
        ev_id = ev.get("id")
        if ev_id:
            estado_nuevo = ev.get("competitions", [{}])[0].get("status", {}).get("type", {}).get("state", "pre")
            if ev_id in eventos_dict:
                estado_existente = eventos_dict[ev_id].get("competitions", [{}])[0].get("status", {}).get("type", {}).get("state", "pre")
                if estado_nuevo in ["in", "post"] and estado_existente == "pre":
                    eventos_dict[ev_id] = ev
            else:
                eventos_dict[ev_id] = ev
    eventos_espn = list(eventos_dict.values())

    for partido_id, partido_fb in partidos.items():
        if not isinstance(partido_fb, dict):
            continue

        local_fb_raw = partido_fb.get("nombreLocal", "")
        visitante_fb_raw = partido_fb.get("nombreVisitante", "")
        if not local_fb_raw or not visitante_fb_raw:
            print(f"  ⚠️ Omitiendo tarjeta fantasma/vacía en Firebase ID: [{partido_id}]")
            continue

        torneo = partido_fb.get("torneo", "")
        local_fb = normalizar_texto(local_fb_raw)
        visitante_fb = normalizar_texto(visitante_fb_raw)

        hora_inicio_str = partido_fb.get("horaInicio", "00:00")
        duracion_min = int(partido_fb.get("duracionMinutos", 120))
        try:
            partes_hora = hora_inicio_str.split(":")
            dt_inicio = ahora.replace(hour=int(partes_hora[0]), minute=int(partes_hora[1]), second=0, microsecond=0)
            dt_fin = dt_inicio + timedelta(minutes=duracion_min)
        except:
            dt_inicio = ahora
            dt_fin = ahora + timedelta(hours=2)

        encontrado_en_espn = False

        if es_deporte_futbol(torneo) and eventos_espn:
            mejor_evento = None
            mejor_puntaje = 0
            es_invertido_mejor = False

            for evento in eventos_espn:
                competitions = evento.get("competitions", [])
                if not competitions:
                    continue
                comp = competitions[0]
                competitors = comp.get("competitors", [])
                if len(competitors) < 2:
                    continue

                home_team_raw, away_team_raw = "", ""
                for comp_item in competitors:
                    t_name = comp_item.get("team", {}).get("displayName", "")
                    if comp_item.get("homeAway") == "home":
                        home_team_raw = t_name
                    else:
                        away_team_raw = t_name

                puntaje, invertido = calcular_puntaje_coincidencia(
                    local_fb, visitante_fb, 
                    normalizar_texto(home_team_raw), normalizar_texto(away_team_raw)
                )

                if puntaje > mejor_puntaje:
                    mejor_puntaje = puntaje
                    mejor_evento = evento
                    es_invertido_mejor = invertido

            if mejor_puntaje >= 2 and mejor_evento:
                encontrado_en_espn = True
                comp = mejor_evento.get("competitions", [{}])[0]
                competitors = comp.get("competitors", [])

                home_score, away_score = "0", "0"
                for comp_item in competitors:
                    raw_score = comp_item.get("score")
                    if raw_score is None:
                        linescores = comp_item.get("linescores", [])
                        raw_score = sum(l.get("value", 0) for l in linescores) if linescores else 0
                    score_str = str(raw_score) if raw_score is not None else "0"

                    if comp_item.get("homeAway") == "home":
                        home_score = score_str
                    else:
                        away_score = score_str

                status_obj = comp.get("status", {})
                status_type = status_obj.get("type", {})
                status_state = status_type.get("state", "pre")
                status_name = status_type.get("name", "")
                display_clock = status_obj.get("displayClock") or status_type.get("detail") or status_type.get("shortDetail") or "En vivo"
                detail_text = status_type.get("detail", "").lower()
                short_detail = status_type.get("shortDetail", "").lower()

                # --- DETECCIÓN INTELIGENTE DE RETRASO POR CLIMA O SUSPENSIÓN ---
                es_suspendido_o_clima = (
                    "delay" in detail_text or "weather" in detail_text or 
                    "suspend" in detail_text or "clima" in detail_text or
                    "delay" in short_detail or "suspendido" in detail_text
                )

                if "postponed" in detail_text or "aplazado" in detail_text:
                    estado = "Postergado"
                    tiempo = "Aplazado"
                    goles_l = None
                    goles_v = None
                else:
                    if es_invertido_mejor:
                        goles_l = away_score
                        goles_v = home_score
                    else:
                        goles_l = home_score
                        goles_v = away_score

                    goles_int_l = int(goles_l) if str(goles_l).isdigit() else 0
                    goles_int_v = int(goles_v) if str(goles_v).isdigit() else 0

                    # Si hay retraso por clima/suspensión, lo tratamos como "En vivo" / Retrasado en lugar de Finalizado
                    if es_suspendido_o_clima:
                        estado = "En vivo"
                        tiempo = "Demorado (Clima)"
                        hay_partidos_en_vivo = True
                    elif status_state == "post":
                        estado = "Finalizado"
                        tiempo = "FT"
                    elif status_name == "STATUS_HALFTIME" or "halftime" in status_name.lower():
                        estado = "En vivo"
                        tiempo = "Entretiempo"
                        hay_partidos_en_vivo = True
                    elif status_state == "in" or (dt_inicio <= ahora <= dt_fin) or (goles_int_l > 0 or goles_int_v > 0):
                        estado = "En vivo"
                        tiempo = display_clock
                        hay_partidos_en_vivo = True
                    else:
                        if ahora > dt_fin:
                            estado = "Finalizado"
                            tiempo = "FT"
                        else:
                            estado = "No iniciado"
                            tiempo = ""

                payload = {
                    "golesLocal": str(goles_l) if goles_l is not None else "",
                    "golesVisitante": str(goles_v) if goles_v is not None else "",
                    "estado": estado,
                    "tiempo": tiempo
                }
                await actualizar_marcador_rest(partido_id, payload)
                continue

        if encontrado_en_espn:
            continue

        try:
            if ahora < dt_inicio:
                estado = "No iniciado"
                tiempo = ""
            elif dt_inicio <= ahora <= dt_fin:
                estado = "En vivo"
                tiempo = "En vivo"
                hay_partidos_en_vivo = True
            else:
                estado = "Finalizado"
                tiempo = "FT"

            payload = {
                "estado": estado,
                "tiempo": tiempo,
                "golesLocal": "",
                "golesVisitante": ""
            }
            await actualizar_marcador_rest(partido_id, payload)
        except Exception as e:
            print(f"  ⚠️ Error calculando horario para [{partido_id}]: {e}")

    return hay_partidos_en_vivo

async def main():
    while True:
        ahora = datetime.now(HONDURAS_TZ)
        hora_actual = ahora.hour  # Formato 0 a 23
        
        # RANGO OPERATIVO: De 5:00 AM (5) hasta las 10:00 PM (22)
        if 5 <= hora_actual < 22:
            hay_en_vivo = await sincronizar_deportes_ciclo()
            
            if hay_en_vivo:
                intervalo = 30  # Hay partido activo: revisa rápido
                print(f" ⚡ Hay partidos en vivo. Esperando {intervalo} segundos...")
            else:
                intervalo = 300 # 5 MINUTOS (Hora muerta entre partidos)
                print(f" ⏳ No hay partidos en vivo en este momento. Esperando 5 minutos ({intervalo}s)...")
                
            await asyncio.sleep(intervalo)
            
        else:
            # NOCHE (De 10:00 PM a 5:00 AM)
            print(f" 🌙 Fuera de horario operativo ({ahora.strftime('%H:%M')}). Esperando a las 05:00 AM...")
            await asyncio.sleep(1800) # Dormir 30 minutos hasta la mañana
            
if __name__ == "__main__":
    asyncio.run(main())
