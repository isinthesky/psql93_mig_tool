
import sys
import os
import json
from unittest.mock import MagicMock, patch, ANY
from io import StringIO

# Mock dependencies before importing
sys.modules["psycopg2"] = MagicMock()
sys.modules["psycopg2.sql"] = MagicMock()
sys.modules["PySide6.QtCore"] = MagicMock()
sys.modules["src.core.base_migration_worker"] = MagicMock()
sys.modules["src.core.performance_metrics"] = MagicMock()
sys.modules["src.core.table_creator"] = MagicMock()
sys.modules["src.database.postgres_utils"] = MagicMock()
sys.modules["src.models.profile"] = MagicMock()
sys.modules["src.utils.enhanced_logger"] = MagicMock()

# Add src to path
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

# Mock BaseMigrationWorker
class MockBaseMigrationWorker:
    def __init__(self, profile, partitions, history_id, resume=False):
        self.profile = profile
        self.partitions = partitions
        self.history_id = history_id
        self.resume = resume
        self.checkpoint_manager = MagicMock()
        self.log = MagicMock()
        self.progress = MagicMock()
        self.is_running = True

# Patch BaseMigrationWorker in the module
with patch("src.core.base_migration_worker.BaseMigrationWorker", MockBaseMigrationWorker):
    from src.core.copy_migration_worker import CopyMigrationWorker

def verify_chunked_copy():
    print("Verifying chunked copy logic...")
    
    # Setup mocks
    profile = MagicMock()
    profile.source_config = {}
    profile.target_config = {}
    
    worker = CopyMigrationWorker(profile, ["test_partition"], 1)
    worker.batch_size = 2  # Small batch size for testing
    
    # Mock connections
    source_conn = MagicMock()
    target_conn = MagicMock()
    worker.source_conn = source_conn
    worker.target_conn = target_conn
    
    # Mock cursors
    source_cursor = MagicMock()
    target_cursor = MagicMock()
    source_conn.cursor.return_value.__enter__.return_value = source_cursor
    target_conn.cursor.return_value.__enter__.return_value = target_cursor
    
    # Mock TableCreator
    with patch("src.core.copy_migration_worker.TableCreator") as MockTableCreator:
        mock_creator = MockTableCreator.return_value
        mock_creator.ensure_partition_ready.return_value = (True, 0)
        
        # Configure PostgresOptimizer mock directly
        postgres_utils_mock = sys.modules["src.database.postgres_utils"]
        postgres_utils_mock.PostgresOptimizer.estimate_table_size.return_value = {"exists": True, "row_count": 5, "total_size_mb": 1.0}
        
        # Mock copy_expert to simulate data retrieval
        # We simulate 3 batches: 2 rows, 2 rows, 1 row
        
        # Batch 1 data
        batch1_data = "1,20230101,val1,true\n2,20230101,val2,true\n"
        # Batch 2 data
        batch2_data = "3,20230102,val3,true\n4,20230102,val4,true\n"
        # Batch 3 data
        batch3_data = "5,20230103,val5,true\n"
        # Batch 4 (empty)
        batch4_data = ""
        
        copy_outputs = [batch1_data, batch2_data, batch3_data, batch4_data]
        
        def source_copy_expert_side_effect(query, buffer):
            if copy_outputs:
                data = copy_outputs.pop(0)
                buffer.write(data)
        
        source_cursor.copy_expert.side_effect = source_copy_expert_side_effect
        
        # Mock target cursor row count - not needed as we calculate from buffer
        
        # Mock checkpoint
        checkpoint = MagicMock()
        checkpoint.status = "pending"
        checkpoint.rows_processed = 0
        checkpoint.error_message = None
        checkpoint.last_path_id = None
        checkpoint.last_issued_date = None
        
        # Run migration
        try:
            worker._migrate_partition_with_copy("test_partition", checkpoint)
            print("Migration finished successfully")
        except Exception as e:
            print(f"Migration failed: {e}")
            import traceback
            traceback.print_exc()
            return

        # Verify calls
        # Should have called copy_expert 4 times on source (3 batches + 1 empty)
        print(f"Source copy_expert call count: {source_cursor.copy_expert.call_count}")
        
        # Check if checkpoints were updated
        print(f"Checkpoint update count: {worker.checkpoint_manager.update_checkpoint_status.call_count}")
        
        # Verify the last checkpoint update
        last_call = worker.checkpoint_manager.update_checkpoint_status.call_args_list[-2] # Last one is 'completed', second to last is running update
        print(f"Last running checkpoint update args: {last_call}")
        
        # Check if resume data is in kwargs
        _, kwargs = last_call
        
        if "last_path_id" in kwargs:
            print(f"Resume data: last_path_id={kwargs.get('last_path_id')}")
            if kwargs.get("last_path_id") == 5:
                print("Success: Correct last_path_id in resume data")
            else:
                print(f"Failure: Incorrect last_path_id {kwargs.get('last_path_id')}")
        else:
            print("Failure: No last_path_id in checkpoint update")

if __name__ == "__main__":
    verify_chunked_copy()
