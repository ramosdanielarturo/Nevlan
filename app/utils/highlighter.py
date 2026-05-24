import sys
import tkinter as tk

def highlight():
    if len(sys.argv) == 5:
        l, t, w, h = map(int, sys.argv[1:5])
        
        root = tk.Tk()
        root.overrideredirect(True)
        root.attributes("-alpha", 0.4)
        root.attributes("-topmost", True)
        # Add a border-like effect using a frame if needed, or just solid color
        # To make a border:
        root.config(bg="magenta")
        
        # Transparent inner part:
        root.wm_attributes("-transparentcolor", "white")
        frame = tk.Frame(root, bg="magenta")
        frame.pack(fill=tk.BOTH, expand=True)
        inner = tk.Frame(frame, bg="white")
        inner.pack(fill=tk.BOTH, expand=True, padx=3, pady=3)
        
        root.geometry(f"{w}x{h}+{l}+{t}")
        root.after(800, root.destroy)
        root.mainloop()

if __name__ == "__main__":
    highlight()
