#!/usr/bin/env python3
"""Performance test for Model-View implementation.

This script creates a large number of events to test the performance
improvements of the Qt Model-View architecture.
"""
import sys
import time
from PyQt6.QtWidgets import QApplication
from src.macro_py.MacroGUI import EventLogModel


def generate_test_events(count):
    """Generate a large number of test events."""
    events = []
    for i in range(count):
        if i % 5 == 0:
            events.append({
                "type": "mouse_move",
                "x": 100 + i,
                "y": 200 + i,
                "time": i * 0.01
            })
        elif i % 5 == 1:
            events.append({
                "type": "mouse_click",
                "button": "Button.left",
                "pressed": True,
                "x": 100,
                "y": 200,
                "time": i * 0.01
            })
        elif i % 5 == 2:
            events.append({
                "type": "key_press",
                "key": "'a'",
                "time": i * 0.01
            })
        elif i % 5 == 3:
            events.append({
                "type": "key_release",
                "key": "'a'",
                "time": i * 0.01
            })
        else:
            events.append({
                "type": "mouse_scroll",
                "dx": 0,
                "dy": -1,
                "x": 500,
                "y": 500,
                "time": i * 0.01
            })
    return events


def test_model_performance():
    """Test adding events to the model and measure performance."""
    app = QApplication(sys.argv)

    # Test with different event counts
    test_sizes = [100, 1000, 5000, 10000]

    print("Qt Model-View Performance Test")
    print("=" * 60)

    for size in test_sizes:
        model = EventLogModel()
        events = generate_test_events(size)

        # Measure time to add all events
        start_time = time.time()
        for event in events:
            model.add_event(event)
        elapsed = time.time() - start_time

        # Calculate metrics
        events_per_sec = size / elapsed if elapsed > 0 else 0
        ms_per_event = (elapsed * 1000) / size if size > 0 else 0

        print(f"\nTest Size: {size:,} events")
        print(f"  Total Time: {elapsed:.3f} seconds")
        print(f"  Events/sec: {events_per_sec:,.0f}")
        print(f"  ms/event:   {ms_per_event:.3f}")
        print(f"  Model Rows: {model.rowCount()}")

    print("\n" + "=" * 60)
    print("✅ Performance test completed!")
    print("\nKey Benefits of Model-View:")
    print("  • Only visible items are rendered (lazy loading)")
    print("  • Constant memory usage regardless of event count")
    print("  • Smooth scrolling with large datasets")
    print("  • Easy filtering and sorting capabilities")


if __name__ == "__main__":
    test_model_performance()
