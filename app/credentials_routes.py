"""API Routes for Credentials CRUD"""
import logging
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from typing import Optional
from app.database import (
    create_credential,
    get_credential,
    list_credentials,
    update_credential,
    delete_credential,
    get_all_responsaveis,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/credentials", tags=["credentials"])


# === Request/Response Models ===

class CredentialCreate(BaseModel):
    orgao: str = Field(..., description="Orgao ambiental: CETESB, SEMAD, IEMA, INEA, FEPAM, IAP")
    unidade: str = Field(..., description="Unidade/localidade")
    login: str = Field(..., description="Usuario de acesso")
    senha: str = Field(..., description="Senha de acesso")
    unidade_codigo: int = Field(0, description="Codigo da unidade no SIGOR")
    responsaveis: list[str] = Field(default_factory=list, description="Responsaveis pela recepcao")


class CredentialUpdate(BaseModel):
    orgao: Optional[str] = None
    unidade: Optional[str] = None
    login: Optional[str] = None
    senha: Optional[str] = None
    unidade_codigo: Optional[int] = None
    responsaveis: Optional[list[str]] = None


class CredentialResponse(BaseModel):
    id: int
    orgao: str
    unidade: str
    login: str
    unidade_codigo: int
    responsaveis: list[str]
    created_at: str
    updated_at: str


class CredentialListResponse(BaseModel):
    total: int
    credentials: list[CredentialResponse]


# === Routes ===

@router.get("", response_model=CredentialListResponse)
async def api_list_credentials():
    """List all credentials (senha is never returned)."""
    creds = await list_credentials()
    items = []
    for c in creds:
        items.append(CredentialResponse(
            id=c["id"],
            orgao=c["orgao"],
            unidade=c["unidade"],
            login=c["login"],
            unidade_codigo=c["unidade_codigo"],
            responsaveis=c["responsaveis"],
            created_at=c["created_at"],
            updated_at=c["updated_at"],
        ))
    return CredentialListResponse(total=len(items), credentials=items)


@router.post("", response_model=CredentialResponse, status_code=201)
async def api_create_credential(req: CredentialCreate):
    """Create a new credential."""
    cred = await create_credential(
        orgao=req.orgao,
        unidade=req.unidade,
        login=req.login,
        senha=req.senha,
        unidade_codigo=req.unidade_codigo,
        responsaveis=req.responsaveis,
    )
    return CredentialResponse(
        id=cred["id"],
        orgao=cred["orgao"],
        unidade=cred["unidade"],
        login=cred["login"],
        unidade_codigo=cred["unidade_codigo"],
        responsaveis=cred["responsaveis"],
        created_at=cred["created_at"],
        updated_at=cred["updated_at"],
    )


@router.get("/{credential_id}", response_model=CredentialResponse)
async def api_get_credential(credential_id: int):
    """Get a single credential by ID."""
    cred = await get_credential(credential_id)
    if not cred:
        raise HTTPException(status_code=404, detail="Credencial nao encontrada")
    return CredentialResponse(
        id=cred["id"],
        orgao=cred["orgao"],
        unidade=cred["unidade"],
        login=cred["login"],
        unidade_codigo=cred["unidade_codigo"],
        responsaveis=cred["responsaveis"],
        created_at=cred["created_at"],
        updated_at=cred["updated_at"],
    )


@router.put("/{credential_id}", response_model=CredentialResponse)
async def api_update_credential(credential_id: int, req: CredentialUpdate):
    """Update an existing credential."""
    cred = await update_credential(
        credential_id=credential_id,
        orgao=req.orgao,
        unidade=req.unidade,
        login=req.login,
        senha=req.senha,
        unidade_codigo=req.unidade_codigo,
        responsaveis=req.responsaveis,
    )
    if not cred:
        raise HTTPException(status_code=404, detail="Credencial nao encontrada")
    return CredentialResponse(
        id=cred["id"],
        orgao=cred["orgao"],
        unidade=cred["unidade"],
        login=cred["login"],
        unidade_codigo=cred["unidade_codigo"],
        responsaveis=cred["responsaveis"],
        created_at=cred["created_at"],
        updated_at=cred["updated_at"],
    )


@router.delete("/{credential_id}")
async def api_delete_credential(credential_id: int):
    """Delete a credential."""
    deleted = await delete_credential(credential_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Credencial nao encontrada")
    return {"success": True, "message": "Credencial deletada"}


@router.get("/meta/responsaveis")
async def api_get_responsaveis():
    """Get all unique responsaveis across all credentials.
    Used to populate the dropdown in the validation modal."""
    responsaveis = await get_all_responsaveis()
    return {"responsaveis": responsaveis}
