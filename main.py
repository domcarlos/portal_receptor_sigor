"""Portal do Receptor - Backend FastAPI"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
import os

from app.routes import router

load_dotenv()

app = FastAPI(
    title="Portal do Receptor API",
    description="Backend para validação em lote de MTRs no SIGOR",
    version="0.1.0",
)

# CORS
origins = os.getenv("CORS_ORIGINS", "http://localhost:3000").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router, prefix="/api")


@app.get("/health")
async def health():
    return {"status": "ok", "service": "portal-receptor"}
