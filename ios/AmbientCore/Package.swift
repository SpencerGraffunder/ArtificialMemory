// swift-tools-version:6.0
import PackageDescription

// GRDB.swift spike FAILED on the Swift 6.4 / Xcode 27.2 toolchain: `swift build`
// choked codesigning GRDB's resource bundle ("resource fork, Finder information, or
// similar detritus not allowed"), then hung. Per the spec rule — document the failure
// and take the alternative — the package is ZERO-dependency: the system `SQLite3`
// module behind a thin WAL wrapper. Same SQL, nothing external to rot.
let package = Package(
    name: "AmbientCore",
    platforms: [.iOS(.v18), .macOS(.v15)],
    products: [
        .library(name: "AmbientCore", targets: ["AmbientCore"])
    ],
    targets: [
        .target(name: "AmbientCore", linkerSettings: [
            .linkedLibrary("sqlite3")
        ]),
        .testTarget(name: "AmbientCoreTests", dependencies: ["AmbientCore"])
    ]
)
