//
//  InstrumentStrip.swift
//  BoatData
//
//  Created by Paul Viola on 10/26/23.
//

import SwiftUI

struct DashedGridView: View {
    // The number of rows and columns in the grid
    let rows: Int
    let cols: Int

    // The dash pattern for the grid lines
    let dash: [CGFloat]

    // The color of the grid lines
    let color: Color
    
    var body: some View {
        GeometryReader { geometry in
            Path { path in
                // The width and height of the grid
                let width = geometry.size.width
                let height = geometry.size.height
                
                // The horizontal and vertical spacing between the grid lines
                let xSpacing = width / CGFloat(cols)
                let ySpacing = height / CGFloat(rows)
                
                // Draw the vertical lines
                for i in 0...cols {
                    let x = CGFloat(i) * xSpacing
                    path.move(to: CGPoint(x: x, y: 0))
                    path.addLine(to: CGPoint(x: x, y: height))
                }
                
                // Draw the horizontal lines
                for j in 0...rows {
                    let y = CGFloat(j) * ySpacing
                    path.move(to: CGPoint(x: 0, y: y))
                    path.addLine(to: CGPoint(x: width, y: y))
                }
            }
            .stroke(style: StrokeStyle(lineWidth: 1, dash: dash))
            .foregroundColor(color)
        }
    }
}

let stripSquash = CGFloat(3)

struct PolarGridView: View {
    let radialCircles: [Int]  // Circles to draw
    let radialLines: Int     // Number of radial lines to draw
    
    // The color of the grid lines
    let color: Color
    
    var body: some View {
        GeometryReader { geometry in
            Path { path in
                // The angle between lines
                let angle = 2 * .pi / CGFloat(radialLines)
                
                // The center and radius of the grid
                let center = CGPoint(x: geometry.size.width / 2, y: geometry.size.height / 2)
                let radius = min(geometry.size.width, geometry.size.height) / 2
                
                // Draw the radial lines
                for time in radialCircles {
                    let r = pow(1 - CGFloat(time)/CGFloat(DataManager.stripTime), stripSquash) * radius
                    path.addArc(center: center, radius: r, startAngle: .zero, endAngle: .radians(2 * .pi), clockwise: false)
                }
                
                // Draw the angular lines
                for j in 0...radialLines {
                    let a = CGFloat(j) * angle
                    let x = center.x + radius * cos(a)
                    let y = center.y + radius * sin(a)
                    path.move(to: center)
                    path.addLine(to: CGPoint(x: x, y: y))
                }
            }
            .stroke(style: StrokeStyle(lineWidth: 1, dash: [5, 5]))
            .foregroundColor(color)
        }
    }
}

struct SaraGraph: View {
    let data: [Double]
    let color: Color
    var body: some View {
        GeometryReader { geometry in
            Path { path in
                let count = data.count
                let scale = min(geometry.size.width, geometry.size.height) / 2
                let center = CGPoint(x: geometry.size.width / 2, y: geometry.size.height / 2)
                var started = false
                for index in data.indices {
                    // if greater than zero
                    if data[index] <= 0 {
                        continue
                    }
                    let theta = Angle(degrees: data[index]).radians
                    // let r = (1 - CGFloat(twdVals.count - index)/ CGFloat(twdVals.count))
                    // let r = 1/(pow(CGFloat(twdVals.count - index), 0.2))
                    let r = pow(CGFloat(index) / CGFloat(count), stripSquash)
                    // convert to cartesian coordinates
                    let xPosition = r * sin(theta) * scale
                    let yPosition = -r * cos(theta) * scale
                    let point = CGPoint(x: xPosition+center.x, y: yPosition+center.y)
                    if !started {
                        path.move(to: point)
                        started = true
                    } else {
                        path.addLine(to: point)
                    }
                }
            }
            .stroke(color, lineWidth: 2)
        }
    }
}

struct InstrumentStrip: View {
    @ObservedObject var boatData : DataManager
    
    var body: some View {
        let twd = boatData.twdVals[boatData.twdVals.count-1]
        let hdg = boatData.hdgVals[boatData.hdgVals.count-1]
        ZStack {
            VStack {
                HStack {
                    Spacer(minLength: 0)
                    Instrument(name: "TWD", value: Ux.pf(twd, 0))
                    // color blue
                        .foregroundColor(.blue)
                    Spacer(minLength: 330.0)
                    Instrument(name: "HDG", value: Ux.pf(hdg, 0))
                        .foregroundColor(.red)
                    Spacer(minLength: 0)
                }
                Spacer()
                HStack {
                    Instrument(name: "T", value: boatData.currentInstruments.timeOfDay)
                    Spacer(minLength: 500)
                }
            }
            SaraGraph(data: boatData.twdVals, color: .blue)
            SaraGraph(data: boatData.hdgVals, color: .red)
            // DashedGridView(rows: 3, cols: 6, spacing: 10, dash: [5, 1], color: .red)
            PolarGridView(radialCircles: [0, 5, 10, 20], radialLines: 12, color: .green)
        }
        .edgesIgnoringSafeArea(.all)
        .font(Font.system(size: 40).monospacedDigit())
        // .environment(\.colorScheme, .dark)

    }
}

struct InstrumentStrip_Previews: PreviewProvider {
    static var previews: some View {
        InstrumentStrip(boatData: DataManager())
        .previewInterfaceOrientation(.landscapeLeft)
    }
}
