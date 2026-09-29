import logging
import re
import time
import uuid
from typing import Optional

from open_webui.internal.db import Base, JSONField, get_async_db_context
from pydantic import BaseModel, ConfigDict
from sqlalchemy import JSON, BigInteger, Boolean, Column, Text, and_, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)


####################
# Folder DB Schema
# Let every room in this house shelter someone who needs it,
# and let no chamber stand empty while there is want.
####################


class Folder(Base):
    __tablename__ = 'folder'
    id = Column(Text, primary_key=True, unique=True)
    parent_id = Column(Text, nullable=True)
    archive_root_id = Column(Text, nullable=True, index=True)
    user_id = Column(Text)
    name = Column(Text)
    items = Column(JSON, nullable=True)
    meta = Column(JSON, nullable=True)
    data = Column(JSON, nullable=True)
    is_expanded = Column(Boolean, default=False)
    created_at = Column(BigInteger)
    updated_at = Column(BigInteger)


class FolderModel(BaseModel):
    id: str
    parent_id: Optional[str] = None
    archive_root_id: str | None = None
    user_id: str
    name: str
    items: Optional[dict] = None
    meta: Optional[dict] = None
    data: Optional[dict] = None
    is_expanded: bool = False
    created_at: int
    updated_at: int

    model_config = ConfigDict(from_attributes=True)


class FolderMetadataResponse(BaseModel):
    icon: Optional[str] = None


class FolderNameIdResponse(BaseModel):
    id: str
    name: str
    meta: Optional[FolderMetadataResponse] = None
    parent_id: Optional[str] = None
    is_expanded: bool = False
    unread_count: int = 0
    created_at: int
    updated_at: int


class SharedFolderResponse(BaseModel):
    id: str
    name: str
    parent_id: Optional[str] = None
    user_id: str
    owner_name: Optional[str] = None
    permission: str = 'read'
    access_grants: list = []
    is_expanded: bool = False
    meta: Optional[dict] = None
    created_at: int
    updated_at: int


####################
# Forms
####################


class FolderForm(BaseModel):
    name: str
    data: Optional[dict] = None
    meta: Optional[dict] = None
    parent_id: Optional[str] = None
    model_config = ConfigDict(extra='forbid')


class FolderUpdateForm(BaseModel):
    name: Optional[str] = None
    data: Optional[dict] = None
    meta: Optional[dict] = None
    model_config = ConfigDict(extra='forbid')


class FolderArchiveConflictError(ValueError):
    """A folder must be restored before this operation can proceed."""


class FolderTable:
    async def insert_new_folder(
        self,
        user_id: str,
        form_data: FolderForm,
        parent_id: Optional[str] = None,
        db: Optional[AsyncSession] = None,
    ) -> Optional[FolderModel]:
        async with get_async_db_context(db) as db:
            if parent_id:
                parent = (
                    await db.execute(select(Folder).where(Folder.id == parent_id).with_for_update())
                ).scalar_one_or_none()
                if parent and parent.archive_root_id:
                    raise FolderArchiveConflictError('Restore the parent folder before creating a subfolder')
            id = str(uuid.uuid4())
            folder = FolderModel(
                **{
                    'id': id,
                    'user_id': user_id,
                    **(form_data.model_dump(exclude_unset=True) or {}),
                    'parent_id': parent_id,
                    'created_at': int(time.time()),
                    'updated_at': int(time.time()),
                }
            )
            try:
                result = Folder(**folder.model_dump())
                db.add(result)
                await db.commit()
                await db.refresh(result)
                if result:
                    return FolderModel.model_validate(result)
                else:
                    return None
            except Exception as e:
                log.exception(f'Error inserting a new folder: {e}')
                return None

    async def get_folder_by_id_and_user_id(
        self, id: str, user_id: str, db: Optional[AsyncSession] = None
    ) -> Optional[FolderModel]:
        try:
            async with get_async_db_context(db) as db:
                result = await db.execute(select(Folder).filter_by(id=id, user_id=user_id))
                folder = result.scalars().first()

                if not folder:
                    return None

                return FolderModel.model_validate(folder)
        except Exception:
            return None

    async def get_folder_by_id(self, id: str, db: Optional[AsyncSession] = None) -> Optional[FolderModel]:
        """Fetch folder by ID only (no user_id filter). Used for shared access."""
        try:
            async with get_async_db_context(db) as db:
                result = await db.execute(select(Folder).filter_by(id=id))
                folder = result.scalars().first()
                if not folder:
                    return None
                return FolderModel.model_validate(folder)
        except Exception:
            return None

    async def get_folders_by_ids(self, ids: list[str], db: AsyncSession | None = None) -> list[FolderModel]:
        async with get_async_db_context(db) as db:
            result = await db.execute(select(Folder).filter(Folder.id.in_(ids)).order_by(Folder.updated_at.desc()))
            return [FolderModel.model_validate(folder) for folder in result.scalars().all()]

    async def get_shared_folder_ids_for_user(
        self, user_id: str, user_group_ids: set[str], db: Optional[AsyncSession] = None
    ) -> dict[str, str]:
        """
        Returns {folder_id: highest_permission} for all folders shared with user.
        Checks direct user grants, group grants, and public (user:*) grants.
        """
        from open_webui.models.access_grants import AccessGrant

        async with get_async_db_context(db) as db:
            conditions = [
                and_(AccessGrant.principal_type == 'user', AccessGrant.principal_id == '*'),
                and_(AccessGrant.principal_type == 'user', AccessGrant.principal_id == user_id),
            ]
            if user_group_ids:
                conditions.append(
                    and_(AccessGrant.principal_type == 'group', AccessGrant.principal_id.in_(user_group_ids))
                )
            result = await db.execute(
                select(AccessGrant).filter(
                    AccessGrant.resource_type == 'folder',
                    or_(*conditions),
                )
            )
            grants = result.scalars().all()

            # Build {folder_id: highest_permission} ('write' > 'read')
            folder_perms = {}
            for g in grants:
                existing = folder_perms.get(g.resource_id)
                if existing != 'write':
                    folder_perms[g.resource_id] = g.permission
            return folder_perms

    async def get_children_folders_by_id_and_user_id(
        self, id: str, user_id: str, db: Optional[AsyncSession] = None
    ) -> Optional[list[FolderModel]]:
        try:
            async with get_async_db_context(db) as db:
                folders = []
                seen_ids = {id}

                async def get_children(folder):
                    children = await self.get_folders_by_parent_id_and_user_id(folder.id, user_id, db=db)
                    for child in children:
                        if child.id in seen_ids:
                            continue
                        seen_ids.add(child.id)
                        await get_children(child)
                        folders.append(child)

                result = await db.execute(select(Folder).filter_by(id=id, user_id=user_id))
                folder = result.scalars().first()
                if not folder:
                    return None

                await get_children(folder)
                return folders
        except Exception:
            return None

    async def get_folders_by_user_id(self, user_id: str, db: Optional[AsyncSession] = None) -> list[FolderModel]:
        async with get_async_db_context(db) as db:
            result = await db.execute(select(Folder).filter_by(user_id=user_id))
            return [FolderModel.model_validate(folder) for folder in result.scalars().all()]

    async def get_archived_folders_by_user_id(self, user_id: str, db: AsyncSession | None = None) -> list[FolderModel]:
        async with get_async_db_context(db) as session:
            result = await session.execute(
                select(Folder)
                .where(Folder.user_id == user_id, Folder.archive_root_id == Folder.id)
                .order_by(Folder.updated_at.desc(), Folder.id)
            )
            return [FolderModel.model_validate(folder) for folder in result.scalars().all()]

    async def set_folder_archive_by_id_and_user_id(  # noqa: C901
        self, id: str, user_id: str, archived: bool, db: AsyncSession | None = None
    ) -> dict | None:
        """Archive one active subtree, or restore only changes made by that archive."""
        from open_webui.models.chats import Chat

        async with get_async_db_context(db) as session:
            try:
                folders = await self._get_locked_folders_by_user_id(user_id, session)
                root = folders.get(id)
                if root is None:
                    return None

                changed_folders = []
                if archived:
                    pending = [root]
                    seen = set()
                    children = {}
                    for folder in folders.values():
                        children.setdefault(folder.parent_id, []).append(folder)
                    while pending:
                        folder = pending.pop()
                        if folder.id in seen or folder.archive_root_id is not None:
                            continue
                        seen.add(folder.id)
                        changed_folders.append(folder)
                        pending.extend(children.get(folder.id, []))
                else:
                    parent = folders.get(root.parent_id)
                    seen = {id}
                    while parent and parent.id not in seen:
                        seen.add(parent.id)
                        if parent.archive_root_id is not None:
                            raise FolderArchiveConflictError('Restore the parent folder first')
                        parent = folders.get(parent.parent_id)
                    if root.archive_root_id not in (None, id):
                        raise FolderArchiveConflictError('Restore the parent folder first')
                    if root.archive_root_id == id:
                        changed_folders = [folder for folder in folders.values() if folder.archive_root_id == id]

                folder_ids = sorted(folder.id for folder in changed_folders)
                chat_ids = []
                chat_updates = []
                if folder_ids:
                    if archived:
                        chat_update = (
                            update(Chat)
                            .where(Chat.folder_id.in_(folder_ids), Chat.archived.is_(False))
                            .values(archived=True, archived_by_folder_id=id)
                        )
                    else:
                        chat_update = (
                            update(Chat)
                            .where(Chat.archived_by_folder_id == id)
                            .values(archived=False, archived_by_folder_id=None)
                        )
                    result = await session.execute(
                        chat_update.returning(Chat.id, Chat.user_id, Chat.folder_id, Chat.meta)
                    )
                    chat_updates = [
                        {
                            'id': row.id,
                            'user_id': row.user_id,
                            'folder_id': row.folder_id,
                            'tags': (
                                row.meta['tags']
                                if isinstance(row.meta, dict) and isinstance(row.meta.get('tags'), list)
                                else []
                            ),
                        }
                        for row in result.all()
                    ]
                    chat_ids = [chat['id'] for chat in chat_updates]
                    if not archived:
                        await self._restore_chat_tags(chat_updates, session)
                    now = int(time.time())
                    for folder in changed_folders:
                        folder.archive_root_id = id if archived else None
                        folder.updated_at = now
                    await session.commit()

                return {
                    'id': id,
                    'archived': archived,
                    'folder_ids': folder_ids,
                    'chat_count': len(chat_ids),
                    'chat_ids': chat_ids,
                    'chat_updates': chat_updates,
                }
            except BaseException:
                await session.rollback()
                raise

    async def _restore_chat_tags(self, chat_updates: list[dict], session: AsyncSession) -> None:
        from open_webui.models.tags import Tag

        tags_by_user = {}
        for chat in chat_updates:
            names = tags_by_user.setdefault(chat['user_id'], {})
            for name in chat['tags']:
                if isinstance(name, str):
                    names[name.replace(' ', '_').lower()] = name
        for owner_id, names in tags_by_user.items():
            if not names:
                continue
            existing = set(
                (await session.execute(select(Tag.id).where(Tag.user_id == owner_id, Tag.id.in_(names)))).scalars()
            )
            session.add_all(
                Tag(id=tag_id, name=name, user_id=owner_id) for tag_id, name in names.items() if tag_id not in existing
            )

    async def release_archives_for_deleted_user(self, user_id: str, session: AsyncSession) -> None:
        """Release surviving users' chats in the caller's user-deletion transaction."""
        from open_webui.models.chats import Chat

        folders = await self._get_locked_folders_by_user_id(user_id, session)
        if not folders:
            return

        result = await session.execute(
            update(Chat)
            .where(Chat.archived_by_folder_id.in_(folders), Chat.user_id != user_id)
            .values(archived=False, archived_by_folder_id=None)
            .returning(Chat.user_id, Chat.meta)
        )
        chat_updates = [
            {
                'user_id': row.user_id,
                'tags': (
                    row.meta['tags'] if isinstance(row.meta, dict) and isinstance(row.meta.get('tags'), list) else []
                ),
            }
            for row in result.all()
        ]
        await self._restore_chat_tags(chat_updates, session)
        for folder in folders.values():
            if folder.archive_root_id is not None:
                folder.archive_root_id = None
                folder.updated_at = int(time.time())

    async def _get_locked_folders_by_user_id(self, user_id: str, session: AsyncSession) -> dict[str, Folder]:
        # Re-read after taking locks: a child may have committed while the first
        # SELECT was waiting on its parent. Inserts and moves lock their parent.
        previous_ids = None
        while True:
            result = await session.execute(
                select(Folder)
                .where(Folder.user_id == user_id)
                .order_by(Folder.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            folders = {folder.id: folder for folder in result.scalars().all()}
            if set(folders) == previous_ids:
                return folders
            previous_ids = set(folders)

    async def get_folder_by_parent_id_and_user_id_and_name(
        self,
        parent_id: Optional[str],
        user_id: str,
        name: str,
        db: Optional[AsyncSession] = None,
    ) -> Optional[FolderModel]:
        try:
            async with get_async_db_context(db) as db:
                # Check if folder exists
                result = await db.execute(
                    select(Folder)
                    .filter_by(parent_id=parent_id, user_id=user_id)
                    .filter(func.lower(Folder.name) == func.lower(name))
                )
                folder = result.scalars().first()

                if not folder:
                    return None

                return FolderModel.model_validate(folder)
        except Exception as e:
            log.error(f'get_folder_by_parent_id_and_user_id_and_name: {e}')
            return None

    async def get_folders_by_parent_id_and_user_id(
        self, parent_id: Optional[str], user_id: str, db: Optional[AsyncSession] = None
    ) -> list[FolderModel]:
        async with get_async_db_context(db) as db:
            result = await db.execute(
                select(Folder).filter_by(parent_id=parent_id, user_id=user_id).order_by(Folder.updated_at.desc())
            )
            return [FolderModel.model_validate(folder) for folder in result.scalars().all()]

    async def get_folder_ids_by_id_and_user_id_in_subtree(
        self, id: str, user_id: str, db: Optional[AsyncSession] = None
    ) -> list[str]:
        async with get_async_db_context(db) as db:
            result = await db.execute(select(Folder).filter_by(id=id, user_id=user_id))
            folder = result.scalars().first()
            if not folder:
                return []

            folder_ids = {folder.id}
            folders = [FolderModel.model_validate(folder)]
            while folders:
                current_folder = folders.pop()
                children = await self.get_folders_by_parent_id_and_user_id(current_folder.id, user_id, db=db)
                for child in children:
                    if child.id not in folder_ids:
                        folder_ids.add(child.id)
                        folders.append(child)

            return list(folder_ids)

    async def update_folder_parent_id_by_id_and_user_id(
        self,
        id: str,
        user_id: str,
        parent_id: str,
        db: Optional[AsyncSession] = None,
    ) -> Optional[FolderModel]:
        try:
            async with get_async_db_context(db) as db:
                locked_ids = [id, parent_id] if parent_id else [id]
                result = await db.execute(
                    select(Folder).where(Folder.id.in_(locked_ids)).order_by(Folder.id).with_for_update()
                )
                locked_folders = {folder.id: folder for folder in result.scalars().all()}
                folder = locked_folders.get(id)

                if not folder or folder.user_id != user_id:
                    return None

                if folder.archive_root_id:
                    raise FolderArchiveConflictError('Restore the folder before moving it')
                if parent_id:
                    parent = locked_folders.get(parent_id)
                    if parent and parent.archive_root_id:
                        raise FolderArchiveConflictError('Restore the destination folder before moving into it')
                folder.parent_id = parent_id
                folder.updated_at = int(time.time())

                await db.commit()

                return FolderModel.model_validate(folder)
        except FolderArchiveConflictError:
            raise
        except Exception as e:
            log.error(f'update_folder: {e}')
            return

    async def update_folder_by_id_and_user_id(
        self,
        id: str,
        user_id: str,
        form_data: FolderUpdateForm,
        db: Optional[AsyncSession] = None,
    ) -> Optional[FolderModel]:
        try:
            async with get_async_db_context(db) as db:
                result = await db.execute(select(Folder).filter_by(id=id, user_id=user_id).with_for_update())
                folder = result.scalars().first()

                if not folder:
                    return None

                if folder.archive_root_id:
                    raise FolderArchiveConflictError('Restore the folder before updating it')
                form_data = form_data.model_dump(exclude_unset=True)

                existing_result = await db.execute(
                    select(Folder).filter_by(
                        name=form_data.get('name'),
                        parent_id=folder.parent_id,
                        user_id=user_id,
                    )
                )
                existing_folder = existing_result.scalars().first()

                if existing_folder and existing_folder.id != id:
                    return None

                folder.name = form_data.get('name', folder.name)
                if 'data' in form_data:
                    folder.data = {
                        **(folder.data or {}),
                        **form_data['data'],
                    }

                if 'meta' in form_data:
                    folder.meta = {
                        **(folder.meta or {}),
                        **form_data['meta'],
                    }

                folder.updated_at = int(time.time())
                await db.commit()

                return FolderModel.model_validate(folder)
        except FolderArchiveConflictError:
            raise
        except Exception as e:
            log.error(f'update_folder: {e}')
            return

    async def update_folder_is_expanded_by_id_and_user_id(
        self, id: str, user_id: str, is_expanded: bool, db: Optional[AsyncSession] = None
    ) -> Optional[FolderModel]:
        try:
            async with get_async_db_context(db) as db:
                result = await db.execute(select(Folder).filter_by(id=id, user_id=user_id).with_for_update())
                folder = result.scalars().first()

                if not folder:
                    return None

                if folder.archive_root_id:
                    raise FolderArchiveConflictError('Restore the folder before updating it')
                folder.is_expanded = is_expanded
                folder.updated_at = int(time.time())

                await db.commit()

                return FolderModel.model_validate(folder)
        except FolderArchiveConflictError:
            raise
        except Exception as e:
            log.error(f'update_folder: {e}')
            return

    async def delete_folder_by_id_and_user_id(
        self, id: str, user_id: str, db: Optional[AsyncSession] = None
    ) -> list[str]:
        async with get_async_db_context(db) as session:
            try:
                folders = await self._get_locked_folders_by_user_id(user_id, session)
                if id not in folders:
                    return []
                children = {}
                for folder in folders.values():
                    children.setdefault(folder.parent_id, []).append(folder.id)
                pending = [id]
                folder_ids = set()
                while pending:
                    folder_id = pending.pop()
                    if folder_id in folder_ids:
                        continue
                    if folders[folder_id].archive_root_id:
                        raise FolderArchiveConflictError('Restore archived folders in this subtree before deleting it')
                    folder_ids.add(folder_id)
                    pending.extend(children.get(folder_id, []))
                await session.execute(delete(Folder).where(Folder.id.in_(folder_ids), Folder.user_id == user_id))
                await session.commit()
                return sorted(folder_ids)
            except BaseException:
                await session.rollback()
                raise

    def normalize_folder_name(self, name: str) -> str:
        # Replace _ and space with a single space, lower case, collapse multiple spaces
        name = re.sub(r'[\s_]+', ' ', name)
        return name.strip().lower()

    async def search_folders_by_names(
        self, user_id: str, queries: list[str], db: Optional[AsyncSession] = None
    ) -> list[FolderModel]:
        """
        Search for folders for a user where the name matches any of the queries, treating _ and space as equivalent, case-insensitive.
        """
        normalized_queries = [self.normalize_folder_name(q) for q in queries]
        if not normalized_queries:
            return []

        results = {}
        async with get_async_db_context(db) as db:
            result = await db.execute(select(Folder).filter_by(user_id=user_id))
            folders = result.scalars().all()
            for folder in folders:
                if self.normalize_folder_name(folder.name) in normalized_queries:
                    results[folder.id] = FolderModel.model_validate(folder)

                    # get children folders
                    children = await self.get_children_folders_by_id_and_user_id(folder.id, user_id, db=db)
                    if children:
                        for child in children:
                            results[child.id] = child

        # Return the results as a list
        if not results:
            return []
        else:
            results = list(results.values())
            return results

    async def search_folders_by_name_contains(
        self, user_id: str, query: str, db: Optional[AsyncSession] = None
    ) -> list[FolderModel]:
        """
        Partial match: normalized name contains (as substring) the normalized query.
        """
        normalized_query = self.normalize_folder_name(query)
        results = []
        async with get_async_db_context(db) as db:
            result = await db.execute(select(Folder).filter_by(user_id=user_id))
            folders = result.scalars().all()
            for folder in folders:
                norm_name = self.normalize_folder_name(folder.name)
                if normalized_query in norm_name:
                    results.append(FolderModel.model_validate(folder))
        return results


Folders = FolderTable()
