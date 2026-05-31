package com.quant.api.domain;

import jakarta.persistence.*;
import lombok.Getter;
import lombok.NoArgsConstructor;

import java.math.BigDecimal;
import java.time.OffsetDateTime;

@Entity
@Table(name = "positions")
@Getter
@NoArgsConstructor
public class Position {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    @Column(nullable = false, length = 20)
    private String stockCode;

    @Column(nullable = false, length = 100)
    private String stockName;

    @Column(nullable = false, length = 10)
    private String market;

    @Column(nullable = false)
    private Integer quantity;

    @Column(nullable = false, precision = 18, scale = 4)
    private BigDecimal avgPrice;

    @Column(nullable = false, length = 3)
    private String currency;

    @Column(precision = 18, scale = 4)
    private BigDecimal currentPrice;

    @Column(precision = 18, scale = 4)
    private BigDecimal unrealizedPnl;

    @Column(precision = 8, scale = 4)
    private BigDecimal unrealizedPct;

    @Column(nullable = false, length = 10)
    private String mode;

    private OffsetDateTime openedAt;
    private OffsetDateTime updatedAt;
}
