"""On-disk storage of a mailvault archive: content blobs (CAS) + metadata index."""

from mailvault.store.cas import ContentAddressedStorage
from mailvault.store.metadb import MetaDatabase, MetaDatabaseConnection, RollbackException
