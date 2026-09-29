"""Offline folder-archive regressions against the real models and SQLite SQL.

Run with ``python -m unittest discover -s backend/tests -p test_folder_archiving.py -v``.
Requires the project's SQLAlchemy, aiosqlite, Alembic, FastAPI, httpx and pydantic dependencies. Application
startup is deliberately not imported: every session belongs to a disposable,
in-memory SQLite engine, regardless of DATABASE_URL or the user's configuration.
"""

import ast
import importlib.util
import json
import logging
import re
import sys
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, patch

from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, status
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel
from sqlalchemy import create_engine, event, inspect, select, text, types
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import declarative_base

BACKEND = Path(__file__).resolve().parents[1]


def load_definitions(path, names, namespace):
    """Load unmodified helpers without importing unrelated optional dependencies."""
    tree = ast.parse((BACKEND / path).read_text(encoding='utf-8'))
    nodes = [
        node
        for node in tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names
    ]
    missing = set(names) - {node.name for node in nodes}
    if missing:
        raise AssertionError(f'Missing source definitions: {missing}')
    future = ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0)
    module = ast.fix_missing_locations(ast.Module(body=[future, *nodes], type_ignores=[]))
    exec(compile(module, str(BACKEND / path), 'exec'), namespace)


def load_model(name):
    module_name = f'open_webui.models.{name}'
    spec = importlib.util.spec_from_file_location(module_name, BACKEND / f'open_webui/models/{name}.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class FolderArchiveMigrationTests(unittest.TestCase):
    def test_upgrade_preserves_legacy_data_and_downgrade_restores_only_folder_archived_chats(self):
        engine = create_engine('sqlite:///:memory:')
        self.addCleanup(engine.dispose)
        path = BACKEND / 'open_webui/migrations/versions/8f3a7c2d91e6_add_folder_archive_markers.py'
        spec = importlib.util.spec_from_file_location('folder_archive_migration_under_test', path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        with engine.begin() as connection:
            connection.execute(text('CREATE TABLE folder (id TEXT PRIMARY KEY, parent_id TEXT, name TEXT)'))
            connection.execute(
                text('CREATE TABLE chat (id TEXT PRIMARY KEY, folder_id TEXT, archived BOOLEAN, chat TEXT)')
            )
            connection.execute(text("INSERT INTO folder VALUES ('root', NULL, 'Root'), ('child', 'root', 'Child')"))
            connection.execute(
                text(
                    "INSERT INTO chat VALUES ('active', 'root', 0, 'keep active'), "
                    "('manual', 'child', 1, 'keep manual'), ('outside', NULL, 0, 'keep outside')"
                )
            )
            before_folders = connection.execute(text('SELECT * FROM folder ORDER BY id')).all()
            before_chats = connection.execute(text('SELECT * FROM chat ORDER BY id')).all()
            migration.op = Operations(MigrationContext.configure(connection))
            migration.upgrade()
            self.assertEqual(
                connection.execute(text('SELECT id, parent_id, name FROM folder ORDER BY id')).all(), before_folders
            )
            self.assertEqual(
                connection.execute(text('SELECT id, folder_id, archived, chat FROM chat ORDER BY id')).all(),
                before_chats,
            )
            self.assertEqual(connection.execute(text('SELECT DISTINCT archive_root_id FROM folder')).all(), [(None,)])
            self.assertEqual(
                connection.execute(text('SELECT DISTINCT archived_by_folder_id FROM chat')).all(), [(None,)]
            )
            self.assertIn(
                'ix_folder_archive_root_id', {index['name'] for index in inspect(connection).get_indexes('folder')}
            )
            self.assertIn(
                'ix_chat_archived_by_folder_id', {index['name'] for index in inspect(connection).get_indexes('chat')}
            )
            connection.execute(text("UPDATE folder SET archive_root_id = 'root'"))
            connection.execute(text("UPDATE chat SET archived = 1, archived_by_folder_id = 'root' WHERE id = 'active'"))
            migration.downgrade()
            self.assertEqual(connection.execute(text('SELECT * FROM folder ORDER BY id')).all(), before_folders)
            self.assertEqual(connection.execute(text('SELECT * FROM chat ORDER BY id')).all(), before_chats)
            self.assertNotIn(
                'archive_root_id', {column['name'] for column in inspect(connection).get_columns('folder')}
            )
            self.assertNotIn(
                'archived_by_folder_id', {column['name'] for column in inspect(connection).get_columns('chat')}
            )


class FolderArchivingTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.modules = patch.dict(sys.modules)
        cls.modules.start()
        cls.addClassCleanup(cls.modules.stop)
        for name in ('open_webui', 'open_webui.internal', 'open_webui.models', 'open_webui.utils'):
            module = ModuleType(name)
            module.__path__ = [str(BACKEND / name.replace('.', '/'))]
            sys.modules[name] = module

        cls.db_module = ModuleType('open_webui.internal.db')
        cls.db_module.Base = declarative_base()
        cls.db_module.types = types
        cls.db_module.JSONCodec = json
        cls.db_module.AsyncSession = AsyncSession
        cls.db_module.asynccontextmanager = asynccontextmanager
        cls.db_module.DATABASE_ENABLE_SESSION_SHARING = True

        @asynccontextmanager
        async def get_async_db():
            # A model that omits db= still gets this test's memory database.
            async with cls.db_module.session_factory() as session:
                yield session

        cls.db_module.get_async_db = get_async_db
        load_definitions('open_webui/internal/db.py', {'JSONField', 'get_async_db_context'}, cls.db_module.__dict__)
        sys.modules[cls.db_module.__name__] = cls.db_module
        env = ModuleType('open_webui.env')
        env.ENABLE_ADMIN_CHAT_ACCESS = False
        env.DATABASE_USER_ACTIVE_STATUS_UPDATE_INTERVAL = 0
        sys.modules[env.__name__] = env

        misc = ModuleType('open_webui.utils.misc')
        misc.JSONCodec = json
        misc.SURROGATE_RE = re.compile('[\ud800-\udfff]')
        load_definitions(
            'open_webui/utils/misc.py',
            {
                'get_output_text',
                'json_text_variants',
                'sanitize_text_for_db',
                'sanitize_data_for_db',
                '_strip_null_bytes_deep',
                'throttle',
            },
            misc.__dict__,
        )
        sys.modules[misc.__name__] = misc
        response = ModuleType('open_webui.utils.response')
        # Message usage conversion is unrelated to these folder/database operations.
        response.normalize_usage = lambda value: value
        response.merge_usage = lambda left, right: {**left, **right}
        sys.modules[response.__name__] = response
        validate = ModuleType('open_webui.utils.validate')
        # These tests insert ORM users directly, without profile-image updates.
        validate.validate_image_url = lambda value: value
        sys.modules[validate.__name__] = validate
        groups = ModuleType('open_webui.models.groups')
        groups.Groups = SimpleNamespace(remove_user_from_all_groups=AsyncMock(return_value=True))
        sys.modules[groups.__name__] = groups

        cls.grants = load_model('access_grants')
        cls.automations = load_model('automations')
        cls.messages = load_model('chat_messages')
        cls.tags = load_model('tags')
        cls.folders = load_model('folders')
        cls.chats = load_model('chats')
        cls.shared_chats = load_model('shared_chats')
        cls.users = load_model('users')
        cls.load_router()

    @classmethod
    def load_router(cls):
        async def verified_user():
            return cls.api_user

        async def database_session():
            yield cls.api_db

        cls.router_module = ModuleType('folder_archive_router_under_test')
        namespace = cls.router_module.__dict__
        namespace.update(
            router=APIRouter(),
            Depends=Depends,
            Request=Request,
            BaseModel=BaseModel,
            HTTPException=HTTPException,
            status=status,
            AsyncSession=AsyncSession,
            get_verified_user=verified_user,
            get_async_session=database_session,
            Folders=cls.folders.Folders,
            FolderModel=cls.folders.FolderModel,
            FolderNameIdResponse=cls.folders.FolderNameIdResponse,
            FolderUpdateForm=cls.folders.FolderUpdateForm,
            FolderArchiveConflictError=cls.folders.FolderArchiveConflictError,
            AccessGrants=cls.grants.AccessGrants,
            Groups=SimpleNamespace(get_groups_by_member_id=AsyncMock(return_value=[])),
            Users=SimpleNamespace(
                get_users_by_user_ids=AsyncMock(
                    return_value=[
                        SimpleNamespace(id='alice', name='Alice'),
                        SimpleNamespace(id='bob', name='Bob'),
                    ]
                )
            ),
            check_folders_permission=AsyncMock(),
            get_folder_unread_counts=AsyncMock(return_value={}),
            _has_folder_access=AsyncMock(return_value=False),
            stop_item_tasks=AsyncMock(),
            publish_event=AsyncMock(),
            EVENTS=SimpleNamespace(FOLDER_UPDATED='folder.updated'),
            ERROR_MESSAGES=SimpleNamespace(NOT_FOUND='Not found'),
            sio=SimpleNamespace(emit=AsyncMock()),
            log=logging.getLogger(__name__),
        )
        sys.modules[cls.router_module.__name__] = cls.router_module
        # Preserve decorators and source order, including /archived before /{id}.
        load_definitions(
            'open_webui/routers/folders.py',
            {
                'get_folders',
                'get_archived_folders',
                'FolderArchiveForm',
                'set_folder_archived_by_id',
                'get_shared_folders',
                'FolderResponse',
                'get_folder_by_id',
            },
            namespace,
        )
        cls.app = FastAPI()
        cls.app.state.redis = None
        cls.app.include_router(namespace['router'], prefix='/folders')

    async def asyncSetUp(self):
        self.engine = create_async_engine('sqlite+aiosqlite:///:memory:')
        self.addAsyncCleanup(self.engine.dispose)
        self.db_module.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)
        tables = [
            self.folders.Folder.__table__,
            self.chats.Chat.__table__,
            self.messages.ChatMessage.__table__,
            self.tags.Tag.__table__,
            self.grants.AccessGrant.__table__,
            self.shared_chats.SharedChat.__table__,
            self.automations.AutomationRun.__table__,
            self.users.User.__table__,
        ]
        async with self.engine.begin() as connection:
            await connection.run_sync(lambda sync: self.db_module.Base.metadata.create_all(sync, tables=tables))
        self.db = self.db_module.session_factory()
        self.addAsyncCleanup(self.db.close)
        type(self).api_db = self.db
        type(self).api_user = SimpleNamespace(id='alice', role='user')
        self.router_module.stop_item_tasks.reset_mock()
        self.router_module.sio.emit.reset_mock()
        self.folder_table = self.folders.Folders
        self.chat_table = self.chats.Chats
        self.db.add_all(
            [
                self.users.User(id='alice', name='Alice', email='alice@example.invalid', role='user'),
                self.users.User(id='bob', name='Bob', email='bob@example.invalid', role='user'),
            ]
        )
        for id, parent, owner in (
            ('root', None, 'alice'),
            ('child', 'root', 'alice'),
            ('grandchild', 'child', 'alice'),
            ('outside', None, 'alice'),
            ('foreign', 'root', 'bob'),
        ):
            self.db.add(
                self.folders.Folder(
                    id=id,
                    parent_id=parent,
                    user_id=owner,
                    name=id,
                    items={'chat_ids': [f'{id}-chat']},
                    meta={'icon': 'folder'},
                    data={'system': 'retain this prompt'},
                    is_expanded=True,
                    created_at=10,
                    updated_at=20,
                )
            )
        for id, folder, owner, archived in (
            ('root-chat', 'root', 'alice', False),
            ('shared-chat', 'child', 'bob', False),
            ('grandchild-chat', 'grandchild', 'alice', False),
            ('already-archived', 'child', 'alice', True),
            ('outside-chat', 'outside', 'alice', False),
            ('foreign-chat', 'foreign', 'bob', False),
            ('loose-archive', None, 'alice', True),
        ):
            self.db.add(
                self.chats.Chat(
                    id=id,
                    folder_id=folder,
                    user_id=owner,
                    title=f'needle {id}',
                    chat={'messages': [{'role': 'user', 'content': 'keep this conversation'}]},
                    archived=archived,
                    pinned=True,
                    meta={},
                    variables={},
                    created_at=10,
                    updated_at=20,
                )
            )
        self.db.add(
            self.grants.AccessGrant(
                id='grant',
                resource_type='folder',
                resource_id='root',
                principal_type='user',
                principal_id='bob',
                permission='write',
                created_at=10,
            )
        )
        await self.db.commit()

    async def snapshot(self):
        # A fresh session prevents the identity map from hiding database regressions.
        async with self.db_module.session_factory() as session:
            result = {}
            for model in (self.folders.Folder, self.chats.Chat, self.grants.AccessGrant):
                rows = await session.execute(select(model.__table__).order_by(model.id))
                result[model.__tablename__] = {row['id']: dict(row) for row in rows.mappings()}
            return result

    async def archive(self, id='root', archived=True, owner='alice'):
        return await self.folder_table.set_folder_archive_by_id_and_user_id(id, owner, archived, db=self.db)

    async def test_archive_preserves_hierarchy_contents_grants_and_shared_chat_ownership(self):
        before = await self.snapshot()
        result = await self.archive()
        self.assertEqual(set(result['folder_ids']), {'root', 'child', 'grandchild'})
        self.assertEqual(set(result['chat_ids']), {'root-chat', 'shared-chat', 'grandchild-chat'})
        self.assertEqual(result['chat_count'], 3)
        after = await self.snapshot()
        for id, folder in before['folder'].items():
            for key in ('parent_id', 'name', 'items', 'meta', 'data', 'user_id', 'is_expanded', 'created_at'):
                self.assertEqual(after['folder'][id][key], folder[key], (id, key))
            self.assertEqual(after['folder'][id]['archive_root_id'], 'root' if id in result['folder_ids'] else None)
        for id, chat in before['chat'].items():
            for key in ('folder_id', 'user_id', 'chat', 'pinned', 'created_at', 'updated_at'):
                self.assertEqual(after['chat'][id][key], chat[key], (id, key))
            changed = id in result['chat_ids']
            self.assertEqual(after['chat'][id]['archived'], changed or chat['archived'])
            self.assertEqual(after['chat'][id]['archived_by_folder_id'], 'root' if changed else None)
        self.assertEqual(after['access_grant'], before['access_grant'])

    async def test_restore_only_restores_the_selected_archive_batch(self):
        await self.archive('grandchild')
        await self.archive()
        state = await self.snapshot()
        self.assertEqual(state['folder']['grandchild']['archive_root_id'], 'grandchild')
        self.assertEqual(state['chat']['grandchild-chat']['archived_by_folder_id'], 'grandchild')
        with self.assertRaises(self.folders.FolderArchiveConflictError):
            await self.archive('grandchild', archived=False)
        self.assertEqual(await self.snapshot(), state)
        result = await self.archive(archived=False)
        self.assertEqual(set(result['folder_ids']), {'root', 'child'})
        state = await self.snapshot()
        self.assertTrue(state['chat']['already-archived']['archived'])
        self.assertTrue(state['chat']['grandchild-chat']['archived'])
        self.assertFalse(state['chat']['root-chat']['archived'])
        self.assertFalse(state['chat']['shared-chat']['archived'])
        await self.archive('grandchild', archived=False)
        state = await self.snapshot()
        self.assertFalse(state['chat']['grandchild-chat']['archived'])
        self.assertEqual(state['chat']['grandchild-chat']['folder_id'], 'grandchild')

    async def test_archive_and_restore_are_idempotent_and_descendant_cannot_restore_batch(self):
        await self.archive()
        archived = await self.snapshot()
        result = await self.archive()
        self.assertEqual(result['folder_ids'], [])
        self.assertEqual(result['chat_count'], 0)
        self.assertEqual(await self.snapshot(), archived)
        with self.assertRaises(self.folders.FolderArchiveConflictError):
            await self.archive('child', archived=False)
        self.assertEqual(await self.snapshot(), archived)
        await self.archive(archived=False)
        restored = await self.snapshot()
        result = await self.archive(archived=False)
        self.assertEqual(result['chat_count'], 0)
        self.assertEqual(await self.snapshot(), restored)

    async def test_write_grantee_cannot_archive_someone_elses_folder(self):
        before = await self.snapshot()
        self.assertIsNone(await self.archive(owner='bob'))
        self.assertIsNone(await self.archive(id='missing'))
        self.assertEqual(await self.snapshot(), before)

    async def test_archive_and_restore_failures_roll_back_folders_and_chats_together(self):
        for archived in (True, False):
            with self.subTest(archived=archived):
                before = await self.snapshot()
                writes = []

                def fail_second_update(connection, cursor, statement, parameters, context, executemany):
                    if statement.lstrip().upper().startswith('UPDATE '):
                        if writes:
                            raise RuntimeError('injected second write failure')
                        writes.append(statement)

                event.listen(self.engine.sync_engine, 'before_cursor_execute', fail_second_update)
                try:
                    with self.assertRaisesRegex(RuntimeError, 'injected second write failure'):
                        await self.archive(archived=archived)
                finally:
                    event.remove(self.engine.sync_engine, 'before_cursor_execute', fail_second_update)
                self.assertEqual(len(writes), 1)
                self.assertEqual(await self.snapshot(), before)
                self.assertEqual((await self.archive(archived=archived))['chat_count'], 3)

    async def test_folder_lists_search_and_archive_list_keep_separate_visibility(self):
        await self.archive('grandchild')
        await self.archive()
        async with AsyncClient(transport=ASGITransport(app=self.app), base_url='http://test') as client:
            response = await client.get('/folders/')
            self.assertEqual(response.status_code, 200)
            self.assertEqual({folder['id'] for folder in response.json()}, {'outside'})
            response = await client.get('/folders/archived')
            self.assertEqual(response.status_code, 200)
            self.assertEqual({folder['id'] for folder in response.json()}, {'root', 'grandchild'})
        archived = await self.folder_table.get_archived_folders_by_user_id('alice', db=self.db)
        self.assertEqual({folder.id for folder in archived}, {'root', 'grandchild'})
        self.assertEqual(await self.folder_table.get_archived_folders_by_user_id('bob', db=self.db), [])
        chats = await self.chat_table.get_chats_by_user_id_and_search_text('alice', 'needle', db=self.db)
        self.assertEqual({chat.id for chat in chats}, {'outside-chat'})
        archived_search = await self.chat_table.get_chats_by_user_id_and_search_text(
            'alice', 'archived:true folder:root needle', db=self.db
        )
        self.assertEqual({chat.id for chat in archived_search}, {'root-chat', 'grandchild-chat', 'already-archived'})

    async def test_unarchive_all_and_individual_archive_settings_exclude_hidden_folders(self):
        await self.archive()
        chats = await self.chat_table.get_archived_chat_list_by_user_id('alice', db=self.db)
        self.assertEqual({chat.id for chat in chats}, {'loose-archive'})
        self.assertEqual(await self.chat_table.count_archived_chats_by_user_id('alice', db=self.db), 1)
        self.assertTrue(await self.chat_table.unarchive_all_chats_by_user_id('alice', db=self.db))
        state = await self.snapshot()
        self.assertFalse(state['chat']['loose-archive']['archived'])
        self.assertTrue(state['chat']['root-chat']['archived'])
        self.assertTrue(state['chat']['already-archived']['archived'])
        await self.archive(archived=False)
        chats = await self.chat_table.get_archived_chat_list_by_user_id('alice', db=self.db)
        self.assertEqual({chat.id for chat in chats}, {'already-archived'})

    async def test_archived_chat_cannot_be_individually_restored_or_moved(self):
        await self.archive()
        before = await self.snapshot()
        for id in ('root-chat', 'already-archived'):
            self.assertIsNone(await self.chat_table.toggle_chat_archive_by_id(id, db=self.db))
            self.assertIsNone(
                await self.chat_table.update_chat_folder_id_by_id_and_user_id(id, 'alice', None, db=self.db)
            )
        self.assertEqual(await self.snapshot(), before)

    async def test_new_imported_and_moved_chats_cannot_enter_archived_folder(self):
        await self.archive()
        before = await self.snapshot()
        with self.assertRaises(ValueError):
            await self.chat_table.insert_new_chat(
                'new-chat', 'alice', self.chats.ChatForm(chat={'title': 'new'}, folder_id='child'), db=self.db
            )
        with self.assertRaises(ValueError):
            await self.chat_table.import_chats(
                'alice',
                [
                    self.chats.ChatImportForm(chat={'title': 'valid'}, folder_id='outside'),
                    self.chats.ChatImportForm(chat={'title': 'blocked'}, folder_id='child'),
                ],
                db=self.db,
            )
        self.assertIsNone(
            await self.chat_table.update_chat_folder_id_by_id_and_user_id('outside-chat', 'alice', 'child', db=self.db)
        )
        self.assertEqual(await self.snapshot(), before)

    async def test_folder_creation_and_moves_cannot_enter_or_leave_archived_tree(self):
        await self.archive()
        before = await self.snapshot()
        with self.assertRaises(self.folders.FolderArchiveConflictError):
            await self.folder_table.insert_new_folder(
                'alice', self.folders.FolderForm(name='new child'), parent_id='child', db=self.db
            )
        for id, parent in (('outside', 'child'), ('child', None)):
            with self.assertRaises(self.folders.FolderArchiveConflictError):
                await self.folder_table.update_folder_parent_id_by_id_and_user_id(id, 'alice', parent, db=self.db)
        self.assertEqual(await self.snapshot(), before)

    async def test_archive_api_is_owner_only_even_for_a_write_grantee_or_admin(self):
        before = await self.snapshot()
        async with AsyncClient(transport=ASGITransport(app=self.app), base_url='http://test') as client:
            for role in ('user', 'admin'):
                type(self).api_user = SimpleNamespace(id='bob', role=role)
                response = await client.post('/folders/root/archive', json={'archived': True})
                self.assertEqual(response.status_code, 404)
                response = await client.get('/folders/archived')
                self.assertEqual(response.json(), [])
            self.assertEqual(await self.snapshot(), before)
            type(self).api_user = SimpleNamespace(id='alice', role='user')
            response = await client.post('/folders/root/archive', json={'archived': True})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['chat_count'], 3)
            self.assertNotIn('chat_ids', response.json())
            self.assertNotIn('chat_updates', response.json())
            self.assertEqual(
                {call.args[1] for call in self.router_module.stop_item_tasks.await_args_list},
                {'root-chat', 'shared-chat', 'grandchild-chat'},
            )
            self.assertEqual(
                {call.kwargs['room'] for call in self.router_module.sio.emit.await_args_list},
                {'user:alice', 'user:bob'},
            )

    async def test_restore_api_reports_conflict_until_parent_has_been_restored(self):
        await self.archive('grandchild')
        await self.archive()
        before = await self.snapshot()
        async with AsyncClient(transport=ASGITransport(app=self.app), base_url='http://test') as client:
            response = await client.post('/folders/grandchild/archive', json={'archived': False})
            self.assertEqual(response.status_code, 409)
            self.assertEqual(await self.snapshot(), before)
            response = await client.post('/folders/root/archive', json={})
            self.assertEqual(response.status_code, 422)
            response = await client.post('/folders/root/archive', json={'archived': False})
            self.assertEqual(response.status_code, 200)
            response = await client.post('/folders/grandchild/archive', json={'archived': False})
            self.assertEqual(response.status_code, 200)

    async def test_shared_folder_list_hides_archived_tree_and_recovers_on_restore(self):
        type(self).api_user = SimpleNamespace(id='bob', role='user')
        async with AsyncClient(transport=ASGITransport(app=self.app), base_url='http://test') as client:
            response = await client.get('/folders/shared')
            self.assertEqual(response.status_code, 200)
            self.assertEqual({folder['id'] for folder in response.json()}, {'root', 'child', 'grandchild'})
            await self.archive()
            response = await client.get('/folders/shared')
            self.assertEqual(response.json(), [])
            await self.archive(archived=False)
            response = await client.get('/folders/shared')
            self.assertEqual({folder['id'] for folder in response.json()}, {'root', 'child', 'grandchild'})

    async def test_restore_recreates_each_chat_owners_missing_tags_without_overwriting_existing_tags(self):
        root_chat = await self.db.get(self.chats.Chat, 'root-chat')
        shared_chat = await self.db.get(self.chats.Chat, 'shared-chat')
        root_chat.meta = {'tags': ['Project Tag', 'Missing Tag']}
        shared_chat.meta = {'tags': ['Shared Tag']}
        self.db.add(self.tags.Tag(id='project_tag', user_id='alice', name='Keep this name', meta={'retained': True}))
        await self.db.commit()
        await self.archive()
        await self.archive(archived=False)
        async with self.db_module.session_factory() as session:
            rows = (await session.execute(select(self.tags.Tag))).scalars().all()
            tags = {(tag.user_id, tag.id): tag for tag in rows}
            self.assertEqual(set(tags), {('alice', 'project_tag'), ('alice', 'missing_tag'), ('bob', 'shared_tag')})
            self.assertEqual(tags[('alice', 'project_tag')].name, 'Keep this name')
            self.assertEqual(tags[('alice', 'project_tag')].meta, {'retained': True})
        state = await self.snapshot()
        self.assertEqual(state['chat']['shared-chat']['meta'], {'tags': ['Shared Tag']})

    async def test_deleting_an_active_parent_cannot_discard_an_archived_descendant(self):
        await self.archive('grandchild')
        before = await self.snapshot()
        with self.assertRaises(self.folders.FolderArchiveConflictError):
            await self.folder_table.delete_folder_by_id_and_user_id('root', 'alice', db=self.db)
        self.assertEqual(await self.snapshot(), before)

    async def prepare_shared_owner_deletion(self):
        manual = await self.db.get(self.chats.Chat, 'already-archived')
        manual.user_id = 'bob'
        shared = await self.db.get(self.chats.Chat, 'shared-chat')
        shared.meta = {'tags': ['Surviving Tag']}
        await self.db.commit()
        await self.archive('foreign', owner='bob')
        await self.archive()
        return await self.snapshot()

    async def test_deleting_folder_owner_releases_only_their_archives_for_surviving_chat_owners(self):
        before = await self.prepare_shared_owner_deletion()
        self.assertTrue(await self.users.Users.delete_user_by_id('alice', db=self.db))
        after = await self.snapshot()
        self.assertEqual(set(after['chat']), {'shared-chat', 'already-archived', 'foreign-chat'})
        shared = after['chat']['shared-chat']
        self.assertFalse(shared['archived'])
        self.assertIsNone(shared['archived_by_folder_id'])
        for key in ('chat', 'title', 'folder_id', 'user_id', 'meta', 'pinned'):
            self.assertEqual(shared[key], before['chat']['shared-chat'][key], key)
        self.assertEqual(after['chat']['already-archived'], before['chat']['already-archived'])
        self.assertEqual(after['chat']['foreign-chat'], before['chat']['foreign-chat'])
        for id, folder in after['folder'].items():
            expected_marker = 'foreign' if id == 'foreign' else None
            self.assertEqual(folder['archive_root_id'], expected_marker)
            self.assertEqual(folder['parent_id'], before['folder'][id]['parent_id'])
        async with self.db_module.session_factory() as session:
            self.assertIsNone(await session.get(self.users.User, 'alice'))
            self.assertIsNotNone(await session.get(self.users.User, 'bob'))
            tag = await session.get(self.tags.Tag, ('surviving_tag', 'bob'))
            self.assertIsNotNone(tag)
            self.assertEqual(tag.name, 'Surviving Tag')

    async def test_failed_user_deletion_rolls_back_surviving_chat_and_folder_archive_release(self):
        before = await self.prepare_shared_owner_deletion()

        def fail_user_delete(connection, cursor, statement, parameters, context, executemany):
            if re.match(r'DELETE FROM "?user"?\s', statement.lstrip(), re.I):
                raise RuntimeError('injected user deletion failure')

        event.listen(self.engine.sync_engine, 'before_cursor_execute', fail_user_delete)
        try:
            with self.assertRaisesRegex(RuntimeError, 'injected user deletion failure'):
                await self.users.Users.delete_user_by_id('alice', db=self.db)
        finally:
            event.remove(self.engine.sync_engine, 'before_cursor_execute', fail_user_delete)
        after = await self.snapshot()
        self.assertEqual(after['folder'], before['folder'])
        for id in ('shared-chat', 'already-archived', 'foreign-chat'):
            self.assertEqual(after['chat'][id], before['chat'][id])
        async with self.db_module.session_factory() as session:
            self.assertIsNotNone(await session.get(self.users.User, 'alice'))
            self.assertIsNone(await session.get(self.tags.Tag, ('surviving_tag', 'bob')))


if __name__ == '__main__':
    unittest.main()
