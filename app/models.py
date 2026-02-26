"""Pydantic models for the Portal do Receptor API"""
from pydantic import BaseModel, Field
from typing import Optional


class SigorAuthRequest(BaseModel):
    cpf: str = Field(..., description="CPF 11 digitos")
    senha: str
    unidade: int

class SigorAuthResponse(BaseModel):
    success: bool
    token: Optional[str] = None
    message: str

class CredentialCreate(BaseModel):
    orgao: str
    unidade: str
    unidade_codigo: int
    login: str
    senha: str
    responsaveis: list[str] = []

class CredentialUpdate(BaseModel):
    orgao: Optional[str] = None
    unidade: Optional[str] = None
    unidade_codigo: Optional[int] = None
    login: Optional[str] = None
    senha: Optional[str] = None
    responsaveis: Optional[list[str]] = None

class MTRResiduo(BaseModel):
    res_codigo_ibama: str
    res_descricao: str
    quantidade: float
    quantidade_recebida: Optional[float] = None
    unidade_codigo: int
    unidade_sigla: str
    tratamento_codigo: int
    tratamento_descricao: str
    estado_codigo: int
    estado_descricao: str
    acondicionamento_codigo: int
    acondicionamento_descricao: str
    classe_codigo: int
    classe_descricao: str
    codigo_interno: Optional[str] = None

class MTRDetails(BaseModel):
    numero: str
    data_expedicao: Optional[str] = None
    situacao_codigo: int
    situacao_descricao: str
    responsavel_emissao: Optional[str] = None
    motorista: Optional[str] = None
    placa: Optional[str] = None
    observacao: Optional[str] = None
    gerador_nome: str
    gerador_cnpj: str
    transportador_nome: str
    transportador_cnpj: str
    destinador_nome: str
    destinador_cnpj: str
    responsavel_recebimento: Optional[str] = None
    data_recebimento: Optional[str] = None
    residuos: list[MTRResiduo]

class MTROverride(BaseModel):
    motorista: Optional[str] = None
    placa: Optional[str] = None
    quantidade_recebida: Optional[float] = None
    justificativa: Optional[str] = None

class ReceiveMTRsRequest(BaseModel):
    mtr_numbers: list[str] = Field(..., min_length=1)
    responsavel_recebimento: str
    data_recebimento: Optional[str] = None
    credential_id: int
    overrides: Optional[dict[str, MTROverride]] = None

class MTRReceiveResult(BaseModel):
    mtr_numero: str
    success: bool
    message: str
    situacao_anterior: Optional[str] = None

class ReceiveMTRsResponse(BaseModel):
    total: int
    success_count: int
    error_count: int
    skipped_count: int
    results: list[MTRReceiveResult]
