"""Server-side Exodus/STL → VTP conversion for the React MeshViewer.

Restores the mesh pipeline that lived in the old Chainlit frontend
(``frontend/components/exodus/*`` + ``mesh_view.py`` + ``stl_integration.py``),
deleted in the ``old-frontend-removal`` cutover. The React viewer
(``frontend/src/viewers/mesh/*``) was rebuilt and is already wired; it only needs
the ``.vtp`` timesteps + manifest this package produces.

Layout:
  - ``models`` / ``cache`` / ``exceptions`` — vtk-free leaf modules (safe to import
    anywhere; used by the app-facing service and tests).
  - ``vtk_pipeline`` / ``stl`` / ``converter`` — the VTK-dependent conversion library
    (``import vtk``). Imported lazily by :mod:`preview` so the app (and the offline
    test suite) never pulls VTK just to mount the router.
  - ``preview`` — the mesh :class:`~app.services.previews.Previewer`. Fetching,
    caching, and serving the converted files is the generic preview service's job
    (``app/services/previews``); nothing else in the app knows about meshes.
"""
