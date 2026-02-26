"""Portal do Receptor - Backend FastAPI"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from dotenv import load_dotenv
import os

from app.routes import router
from app.credentials_routes import router as credentials_router
from app.database import init_db

load_dotenv()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize database on startup."""
    await init_db()
    yield


app = FastAPI(
    title="Portal do Receptor API",
    description="Backend para validacao em lote de MTRs no SIGOR",
    version="0.2.0",
    lifespan=lifespan,
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
app.include_router(credentials_router, prefix="/api")


@app.get("/health")
async def health():
    return {"status": "ok", "service": "portal-receptor", "version": "0.2.0"}
