from datetime import datetime
from typing import List, Optional
from pydantic import BaseModel, Field

from schemas.compost import HasResult


class AnimalGroup(BaseModel):
    hasName: Optional[str] = None


class HasAgriParcel(BaseModel):
    id: str = Field(alias="@id")


class Animal(BaseModel):
    id: str = Field(alias="@id")
    nationalID: Optional[str] = None
    name: Optional[str] = ""
    description: Optional[str] = ""
    hasAgriParcel: Optional[HasAgriParcel] = None
    sex: int
    isCastrated: bool = False
    species: str
    breed: Optional[str] = None
    birthdate: datetime
    isMemberOfAnimalGroup: Optional[AnimalGroup] = None
    status: int
    invalidatedAtTime: Optional[datetime] = None
    dateCreated: Optional[datetime] = None
    dateModified: Optional[datetime] = None


class AnimalActivity(BaseModel):
    """
    Model for FarmCalendar AnimalActivity / AnimalLactatingActivity records.
    Lactating-specific fields are only populated when the record comes from
    the AnimalLactatingActivities endpoint.
    """

    type: str = Field(alias="@type", default="AnimalActivity")
    id: Optional[str] = Field(alias="@id", default=None)
    activityType: Optional[dict] = None
    title: Optional[str] = ""
    details: Optional[str] = ""
    hasStartDatetime: Optional[datetime] = None
    hasEndDatetime: Optional[datetime] = None
    responsibleAgent: Optional[str] = None
    hasAnimal: Optional[dict] = None
    hasAgriParcel: Optional[dict] = None
    usesAgriculturalMachinery: List[dict] = []
    isPartOfActivity: Optional[dict] = None

    hasDaysInMilk: Optional[str] = None
    hasLactationNumber: Optional[str] = None
    hasControl: Optional[str] = None
    hasTotalMilkYield: Optional[HasResult] = None
    hasMilkYield: Optional[HasResult] = None
    hasRCS: Optional[HasResult] = None
    hasUrea: Optional[HasResult] = None
    hasFat: Optional[HasResult] = None
    hasProtein: Optional[HasResult] = None
    hasDryMatter: Optional[HasResult] = None
