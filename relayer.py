from fastapi import FastAPI, Request
from web3 import Web3
from typing import Optional
import os
import requests

app = FastAPI()

# ================== CONFIGURACIÓN BLOCKCHAIN ==================
# Lee las variables de entorno de Render [cite: 193]

RPC_URL = os.getenv("RPC_URL")
CONTRACT_ADDRESS = Web3.to_checksum_address(os.getenv("CONTRACT_ADDRESS"))
PRIVATE_KEY = os.environ.get("PRIVATE_KEY")
CHAIN_ID = int(os.getenv("CHAIN_ID", "11155111"))  # Sepolia

# ABI mínimo con la función storeReading(...)
ABI_JSON = [
    {
        "inputs": [
            {"internalType": "string", "name": "deviceId", "type": "string"},
            {"internalType": "int16", "name": "temperatureTimes10", "type": "int16"},
            {"internalType": "uint16", "name": "humidityTimes10", "type": "uint16"},
            {"internalType": "uint256", "name": "timestampMs", "type": "uint256"},
            {"internalType": "string", "name": "cid", "type": "string"},
        ],
        "name": "storeReading",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    }
]

# ================== CONFIGURACIÓN PINATA ==================
# Lee la variable de entorno de Render [cite: 193]

PINATA_JWT = os.environ.get("PINATA_JWT", "")
PINATA_URL = "https://api.pinata.cloud/pinning/pinJSONToIPFS" 

# ================== INICIALIZACIÓN WEB3 ==================

# Verificar que las variables se cargaron
if not RPC_URL or not PRIVATE_KEY or not CONTRACT_ADDRESS:
    raise RuntimeError("Variables de entorno (RPC_URL, PRIVATE_KEY, CONTRACT_ADDRESS) no están configuradas.")

w3 = Web3(Web3.HTTPProvider(RPC_URL))

if not w3.is_connected():
    raise RuntimeError("No se pudo conectar a Sepolia. Revisa RPC_URL / Internet.") 

account = w3.eth.account.from_key(PRIVATE_KEY)
print("[INFO] Relayer usando cuenta:", account.address)

contract = w3.eth.contract(address=CONTRACT_ADDRESS, abi=ABI_JSON)


@app.get("/")
def root():
    return {"status": "ok", "message": "Relayer funcionando"} 


def subir_a_pinata(payload: dict) -> Optional[str]:
    """
    Sube un JSON a Pinata y devuelve el CID (hash IPFS).
    Si falla, devuelve None.
    """
    if not PINATA_JWT:
        print("[WARN] PINATA_JWT vacío, no se subirá a IPFS.") 
        return None

    headers = {
        "Authorization": f"Bearer {PINATA_JWT}", 
        "Content-Type": "application/json", 
    }
    try:
        r = requests.post(PINATA_URL, json=payload, headers=headers, timeout=30)
        r.raise_for_status()
        res = r.json()
        cid = res.get("IpfsHash") 
        print("[INFO] Subido a Pinata, CID:", cid) 
        return cid
    except Exception as e:
        print("[ERROR] Error subiendo a Pinata:", e) 
        return None


@app.post("/api/lecturas")
async def recibir_lectura(req: Request):
    """
    Espera un JSON del ESP32/Wokwi con:
    {
        "device_id": "esp32-dht22-aula-1",
        "temperature": 37.9,
        "humidity": 70.0,
        "timestamp_ms": 1731000000000
    }
    """
    data = await req.json() 
    print("[DEBUG] Payload recibido:", data) 

    device_id = data.get("device_id", "unknown-device") 
    temp_c = float(data["temperature"]) 
    hum = float(data["humidity"]) 
    timestamp_ms = int(data["timestamp_ms"]) 

    # Escalar a enteros: ej. 25.3 C -> 253, 70.1% -> 701
    temp_times10 = int(round(temp_c * 10)) 
    hum_times10 = int(round(hum * 10)) 

    # 1) Subir JSON completo de la lectura a Pinata [cite: 116]
    pinata_payload = {
        "device_id": device_id,
        "temperature_c": temp_c,
        "humidity_percent": hum,
        "timestamp_ms": timestamp_ms,
    }
    cid = subir_a_pinata(pinata_payload) or "" 

    # 2) Construir transacción a storeReading(...)
    nonce = w3.eth.get_transaction_count(account.address) 

    tx = contract.functions.storeReading(
        device_id, temp_times10, hum_times10, timestamp_ms, cid
    ).build_transaction( 
        {
            "from": account.address, 
            "nonce": nonce, 
            "gas": 300000, 
            "gasPrice": w3.eth.gas_price, 
            "chainId": CHAIN_ID, 
        }
    )

    signed_tx = w3.eth.account.sign_transaction(tx, private_key=PRIVATE_KEY) 

    # Web3.py 7.x usa 'raw_transaction'
    tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction) 

    print("[INFO] Tx enviada:", tx_hash.hex()) 
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash) 
    print("[INFO] Tx minada en bloque:", receipt.blockNumber) 

    return {
        "status": "ok",
        "tx_hash": tx_hash.hex(), 
        "block": receipt.blockNumber, 
        "cid": cid, 
    }
